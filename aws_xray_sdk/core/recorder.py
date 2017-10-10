import logging
import time
import os
import traceback
import json

import wrapt

from .models.segment import Segment
from .models.subsegment import Subsegment
from .models.default_dynamic_naming import DefaultDynamicNaming
from .models.dummy_entities import DummySegment, DummySubsegment
from .emitters.udp_emitter import UDPEmitter
from .sampling.default_sampler import DefaultSampler
from .context import Context
from .plugins.utils import get_plugin_modules
from .lambda_launcher import check_in_lambda
from .exceptions.exceptions import SegmentNameMissingException
from .utils.compat import string_types

log = logging.getLogger(__name__)

TRACING_NAME_KEY = 'AWS_XRAY_TRACING_NAME'
DAEMON_ADDR_KEY = 'AWS_XRAY_DAEMON_ADDRESS'
CONTEXT_MISSING_KEY = 'AWS_XRAY_CONTEXT_MISSING'


class AWSXRayRecorder(object):
    """
    A global AWS X-Ray recorder that will begin/end segments/subsegments
    and send them to the X-Ray daemon. This recorder is initialized during
    loading time so you can use::
        from aws_xray_sdk.core import xray_recorder
    in your module to access it
    """
    def __init__(self):

        context = check_in_lambda()
        if context:
            self._context = context
            self._max_subsegments = 0
        else:
            self._context = Context()
            self._max_subsegments = 30

        self._emitter = UDPEmitter()
        self._sampler = DefaultSampler()
        self._sampling = True
        self._max_trace_back = 10
        self._plugins = None
        self._service = os.getenv(TRACING_NAME_KEY)
        self._dynamic_naming = None

    def configure(self, sampling=None, plugins=None,
                  context_missing=None, sampling_rules=None,
                  daemon_address=None, service=None,
                  context=None, emitter=None,
                  dynamic_naming=None):
        """Configure global X-Ray recorder.

        Configure needs to run before patching thrid party libraries
        to avoid creating dangling subsegment.

        :param bool sampling: If sampling is enabled, every time the recorder
            creates a segment it decides whether to send this segment to
            the X-Ray daemon. This setting is not used if the recorder
            is running in AWS Lambda.
        :param sampling_rules: Pass a set of custom sampling rules.
            Can be an absolute path of the sampling rule config json file
            or a dictionary that defines those rules.
        :param tuple plugins: plugins that add extra metadata to each segment.
            Currently available plugins are EC2Plugin, ECS plugin and
            ElasticBeanstalkPlugin.
        :param str context_missing: recorder behavior when it tries to mutate
            a segment or add a subsegment but there is no active segment.
            RUNTIME_ERROR means the recorder will raise an exception.
            LOG_ERROR means the recorder will only log the error and
            do nothing.
        :param str daemon_address: The X-Ray daemon address where the recorder
            sends data to.
        :param str service: default segment name if creating a segment without
            providing a name.
        :param context: You can pass your own implementation of context storage
            for active segment/subsegment by overriding the default
            ``Context`` class.
        :param emitter: The emitter that sends a segment/subsegment to
            the X-Ray daemon. You can override ``UDPEmitter`` class.
        :param dynamic_naming: a string that defines a pattern that host names
            should match. Alternatively you can pass a module which
            overrides ``DefaultDynamicNaming`` module.

        Environment variables AWS_XRAY_DAEMON_ADDRESS, AWS_XRAY_CONTEXT_MISSING
        and AWS_XRAY_TRACING_NAME respectively overrides arguments
        daemon_address, context_missing and service.
        """
        if sampling is not None:
            self.sampling = sampling
        if service:
            self.service = os.getenv(TRACING_NAME_KEY, service)
        if sampling_rules:
            self._load_sampling_rules(sampling_rules)
        if emitter:
            self.emitter = emitter
        if daemon_address:
            self.emitter.set_daemon_address(os.getenv(DAEMON_ADDR_KEY, daemon_address))
        if context:
            self.context = context
        if context_missing:
            self.context.context_missing = os.getenv(CONTEXT_MISSING_KEY, context_missing)
        if dynamic_naming:
            self.dynamic_naming = dynamic_naming

        plugin_modules = None
        if plugins:
            plugin_modules = get_plugin_modules(plugins)
            for module in plugin_modules:
                module.initialize()
        self._plugins = plugin_modules

    def begin_segment(self, name=None, traceid=None,
                      parent_id=None, sampling=None):
        """
        Begin a segment on the current thread and return it. The recorder
        only keeps one segment at a time. Create the second one without
        closing existing one will overwrite it.

        :param str name: the name of the segment
        :param str traceid: trace id of the segment
        :param int sampling: 0 means not sampled, 1 means sampled
        """
        seg_name = name or self.service
        if not seg_name:
            raise SegmentNameMissingException("Segment name is required.")

        # we respect sampling decision regardless of recorder configuration.
        dummy = False
        if sampling == 0:
            dummy = True
        elif sampling == 1:
            dummy = False
        elif self.sampling and not self._sampler.should_trace():
            dummy = True

        if dummy:
            segment = DummySegment(seg_name)
        else:
            segment = Segment(name=seg_name, traceid=traceid,
                              parent_id=parent_id)
            self._populate_runtime_context(segment)

        self.context.put_segment(segment)
        return segment

    def end_segment(self, end_time=None):
        """
        End the current segment and send it to X-Ray daemon
        if it is ready to send. Ready means segment and
        all its subsegments are closed.

        :param float end_time: segment compeletion in unix epoch in seconds.
        """
        self.context.end_segment(end_time)
        if self.current_segment().ready_to_send():
            self._send_segment()

    def current_segment(self):
        """
        Return the currently active segment. In a multithreading environment,
        this will make sure the segment returned is the one created by the
        same thread.
        """
        entity = self.get_trace_entity()
        if self._is_subsegment(entity):
            return entity.parent_segment
        else:
            return entity

    def begin_subsegment(self, name, namespace='local'):
        """
        Begin a new subsegment.
        If there is open subsegment, the newly created subsegment will be the
        child of latest opened subsegment.
        If not, it will be the child of the current open segment.

        :param str name: the name of the subsegment.
        :param str namespace: currently can only be 'local', 'remote', 'aws'.
        """
        segment = self.current_segment()
        if not segment:
            log.warning("No segment found, cannot begin subsegment %s." % name)
            return None

        if not segment.sampled:
            subsegment = DummySubsegment(segment, name)
        else:
            subsegment = Subsegment(name, namespace, segment)

        self.context.put_subsegment(subsegment)

        return subsegment

    def current_subsegment(self):
        """
        Return the latest opened subsegment. In a multithreading environment,
        this will make sure the subsegment returned is one created
        by the same thread.
        """
        entity = self.get_trace_entity()
        if self._is_subsegment(entity):
            return entity
        else:
            return None

    def end_subsegment(self, end_time=None):
        """
        End the current active subsegment. If this is the last one open
        under its parent segment, the entire segment will be sent.

        :param float end_time: subsegment compeletion in unix epoch in seconds.
        """
        if not self.context.end_subsegment(end_time):
            return

        # if segment is already close, we check if we can send entire segment
        # otherwise we check if we need to stream some subsegments
        if self.current_segment().ready_to_send():
            self._send_segment()
        else:
            self.stream_subsegments()

    def get_trace_entity(self):
        """
        A pass through method to ``context.get_trace_entity()``.
        """
        return self.context.get_trace_entity()

    def set_trace_entity(self, trace_entity):
        """
        A pass through method to ``context.set_trace_entity()``.
        """
        self.context.set_trace_entity(trace_entity)

    def clear_trace_entities(self):
        """
        A pass through method to ``context.clear_trace_entities()``.
        """
        self.context.clear_trace_entities()

    def stream_subsegments(self):
        """
        Stream all closed subsegments to the daemon
        and remove reference to the parent segment.
        No-op for a not sampled segment.
        """
        segment = self.current_segment()

        if not segment or not segment.sampled:
            return

        if segment.get_total_subsegments_size() <= self._max_subsegments:
            return

        # find all subsegments that has no open child subsegments and
        # send them to the daemon
        self._stream_eligible_subsegments(segment)

    def _stream_eligible_subsegments(self, subsegment):

        children = subsegment.subsegments

        children_ready = []
        if len(children) > 0:
            for child in children:
                if self._stream_eligible_subsegments(child):
                    children_ready.append(child)

        if len(children_ready) == len(children) and not subsegment.in_progress:
            return True

        # stream all ready children before return False
        for child in children_ready:
            self._stream_subsegment(child)
            subsegment.remove_subsegment(child)

        return False

    def capture(self, name=None):
        """
        A decorator that records enclosed function in a subsegment.
        It only works with synchronous functions.

        params str name: The name of the subsegment. If not specified
        the function name will be used.
        """
        @wrapt.decorator
        def wrapper(wrapped, instance, args, kwargs):
            func_name = name
            if not func_name:
                func_name = wrapped.__name__

            return self.record_subsegment(
                wrapped, instance, args, kwargs,
                name=func_name,
                namespace='local',
                meta_processor=None,
            )

        return wrapper

    def record_subsegment(self, wrapped, instance, args, kwargs, name,
                          namespace, meta_processor):

        subsegment = self.begin_subsegment(name, namespace)

        try:
            return_value = wrapped(*args, **kwargs)
            exception = None
            stack = None
            return return_value
        except Exception as e:
            exception = e
            stack = traceback.extract_stack(limit=self._max_trace_back)
            return_value = None
            raise
        finally:
            end_time = time.time()
            if callable(meta_processor):
                meta_processor(
                    wrapped=wrapped,
                    instance=instance,
                    args=args,
                    kwargs=kwargs,
                    return_value=return_value,
                    exception=exception,
                    subsegment=subsegment,
                    stack=stack,
                )
            elif exception:
                if subsegment:
                    subsegment.add_exception(exception, stack)

            self.end_subsegment(end_time)

    def _populate_runtime_context(self, segment):
        if not self._plugins:
            return

        aws_meta = {}
        for plugin in self._plugins:
            if plugin.runtime_context:
                aws_meta[plugin.SERVICE_NAME] = plugin.runtime_context
                setattr(segment, 'origin', plugin.ORIGIN)
        segment.set_aws(aws_meta)

    def _send_segment(self):
        """
        Send the current segment to X-Ray daemon if it is present and
        sampled, then clean up context storage.
        The emitter will handle failures.
        """
        segment = self.current_segment()

        if not segment:
            return

        if segment.sampled:
            self.emitter.send_entity(segment)
        self.clear_trace_entities()

    def _stream_subsegment(self, subsegment):

        log.debug("streaming subsegments...")
        self.emitter.send_entity(subsegment)

    def _load_sampling_rules(self, sampling_rules):

        if not sampling_rules:
            return

        if isinstance(sampling_rules, dict):
            self.sampler = DefaultSampler(sampling_rules)
        else:
            with open(sampling_rules) as f:
                self.sampler = DefaultSampler(json.load(f))

    def _is_subsegment(self, entity):

        return (hasattr(entity, 'type') and entity.type == 'subsegment')

    @property
    def sampling(self):
        return self._sampling

    @sampling.setter
    def sampling(self, value):
        self._sampling = value

    @property
    def sampler(self):
        return self._sampler

    @sampler.setter
    def sampler(self, value):
        self._sampler = value

    @property
    def service(self):
        return self._service

    @service.setter
    def service(self, value):
        self._service = value

    @property
    def dynamic_naming(self):
        return self._dynamic_naming

    @dynamic_naming.setter
    def dynamic_naming(self, value):
        if isinstance(value, string_types):
            self._dynamic_naming = DefaultDynamicNaming(value, self.service)
        else:
            self._dynamic_naming = value

    @property
    def context(self):
        return self._context

    @context.setter
    def context(self, cxt):
        self._context = cxt

    @property
    def emitter(self):
        return self._emitter

    @emitter.setter
    def emitter(self, value):
        self._emitter = value
