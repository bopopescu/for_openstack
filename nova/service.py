# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
# Copyright 2011 Justin Santa Barbara
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""Generic Node base class for all workers that run on hosts."""

import os
import random
import sys

from oslo_concurrency import processutils
from oslo_log import log as logging
import oslo_messaging as messaging
from oslo_service import service
from oslo_utils import importutils

from nova import baserpc
from nova import conductor
import nova.conf
from nova import context
from nova import debugger
from nova import exception
from nova.i18n import _, _LE, _LI, _LW
from nova import objects
from nova.objects import base as objects_base
from nova.objects import service as service_obj
from nova import rpc
from nova import servicegroup
from nova import utils
from nova import version
from nova import wsgi

osprofiler = importutils.try_import("osprofiler")
osprofiler_initializer = importutils.try_import("osprofiler.initializer")


LOG = logging.getLogger(__name__)

CONF = nova.conf.CONF

SERVICE_MANAGERS = {
    'nova-compute': 'nova.compute.manager.ComputeManager',
    'nova-console': 'nova.console.manager.ConsoleProxyManager',
    'nova-consoleauth': 'nova.consoleauth.manager.ConsoleAuthManager',
    'nova-conductor': 'nova.conductor.manager.ConductorManager',
    'nova-metadata': 'nova.api.manager.MetadataManager',
    'nova-scheduler': 'nova.scheduler.manager.SchedulerManager',
    'nova-cells': 'nova.cells.manager.CellsManager',
}


def _create_service_ref(this_service, context):
    service = objects.Service(context)
    service.host = this_service.host
    service.binary = this_service.binary
    service.topic = this_service.topic
    service.report_count = 0
    service.create()
    return service


def _update_service_ref(service):
    if service.version != service_obj.SERVICE_VERSION:
        LOG.info(_LI('Updating service version for %(binary)s on '
                     '%(host)s from %(old)i to %(new)i'),
                 {'binary': service.binary,
                  'host': service.host,
                  'old': service.version,
                  'new': service_obj.SERVICE_VERSION})
        service.version = service_obj.SERVICE_VERSION
        service.save()


def setup_profiler(binary, host):
    if osprofiler and CONF.profiler.enabled:
        osprofiler.initializer.init_from_conf(
            conf=CONF,
            context=context.get_admin_context().to_dict(),
            project="nova",
            service=binary,
            host=host)
        LOG.info(_LI("OSProfiler is enabled."))

    #eventlet使用的时间类型判断是否为CLOCK_MONTONIC，CLOCK_REALTIME，可以理解为wall time，即是实际的时间；CLOCK_MONTONIC，是单调时间，即从某个时间点开始到现在过去的时间；
def assert_eventlet_uses_monotonic_clock():
    import eventlet.hubs as hubs
    import monotonic

    hub = hubs.get_hub()
    if hub.clock is not monotonic.monotonic:
        raise RuntimeError(
            'eventlet hub is not using a monotonic clock - '
            'periodic tasks will be affected by drifts of system time.')

#启动的主要功能：1、rpc服务监听消费，主要调用manager;2、周期性任务，上报服务的状态,用tg.add_timer；3、manger中周期性任务的启动，循环执行，用tg.add_dynamic_timer
class Service(service.Service):
    """Service object for binaries running on hosts.

    A service takes a manager and enables rpc by listening to queues based
    on topic. It also periodically runs tasks on the manager and reports
    its state to the database services table.
    """

    def __init__(self, host, binary, topic, manager, report_interval=None,
                 periodic_enable=None, periodic_fuzzy_delay=None,
                 periodic_interval_max=None, *args, **kwargs):
        super(Service, self).__init__()
        self.host = host        #主机ip
        self.binary = binary    #服务名字
        self.topic = topic      #RPC
        self.manager_class_name = manager    #_1.1功能模块名称
        self.servicegroup_api = servicegroup.API()  #服务状态上报db相关
        manager_class = importutils.import_class(self.manager_class_name)  #_1.2动态加载manager模块中的manager_class
        self.manager = manager_class(host=self.host, *args, **kwargs)  #_1.3实例化manager_class
        self.rpcserver = None
        self.report_interval = report_interval   #向数据库汇报状态相关参数
        self.periodic_enable = periodic_enable   #向数据库汇报状态相关参数
        self.periodic_fuzzy_delay = periodic_fuzzy_delay        #向数据库汇报状态相关参数
        self.periodic_interval_max = periodic_interval_max      #向数据库汇报状态相关参数
        self.saved_args, self.saved_kwargs = args, kwargs
        self.backdoor_port = None
        if objects_base.NovaObject.indirection_api:         #默认indirection_api = None
            conductor_api = conductor.API()
            conductor_api.wait_until_ready(context.get_admin_context())
        setup_profiler(binary, self.host)  #分析器相关 ？？？

    def __repr__(self):
        return "<%(cls_name)s: host=%(host)s, binary=%(binary)s, " \
               "manager_class_name=%(manager)s>" % {
                 'cls_name': self.__class__.__name__,
                 'host': self.host,
                 'binary': self.binary,
                 'manager': self.manager_class_name
                }

    def start(self):
        assert_eventlet_uses_monotonic_clock() #eventlet使用的时间类型判断是否为CLOCK_MONTONIC

        verstr = version.version_string_with_package()  #版本信息  ？？？   log用
        LOG.info(_LI('Starting %(topic)s node (version %(version)s)'),
                  {'topic': self.topic, 'version': verstr})
        self.basic_config_check()     #尝试temp文件的创建与删除操作
        self.manager.init_host()
        self.model_disconnected = False
        ctxt = context.get_admin_context()    #待续，，，，，，，，，，，，，，，，
        self.service_ref = objects.Service.get_by_host_and_binary(
            ctxt, self.host, self.binary)       #从数据库获取service对象
        if self.service_ref:
            _update_service_ref(self.service_ref)

        else:
            try:
                self.service_ref = _create_service_ref(self, ctxt)
            except (exception.ServiceTopicExists,
                    exception.ServiceBinaryExists):
                # NOTE(danms): If we race to create a record with a sibling
                # worker, don't fail here.
                self.service_ref = objects.Service.get_by_host_and_binary(
                    ctxt, self.host, self.binary)

        self.manager.pre_start_hook()

        if self.backdoor_port is not None:
            self.manager.backdoor_port = self.backdoor_port

        LOG.debug("Creating RPC server for service %s", self.topic)

        target = messaging.Target(topic=self.topic, server=self.host)

        endpoints = [
            self.manager,
            baserpc.BaseRPCAPI(self.manager.service_name, self.backdoor_port)
        ]
        endpoints.extend(self.manager.additional_endpoints)

        serializer = objects_base.NovaObjectSerializer()    #序列化、反序列化

        self.rpcserver = rpc.get_server(target, endpoints, serializer)  #根据target生成prc消费者，调用endpoints中对象方法处理返回结果。
        self.rpcserver.start()

        self.manager.post_start_hook()

        LOG.debug("Join ServiceGroup membership for this service %s",
                  self.topic)
        # Add service to the ServiceGroup membership group.
        self.servicegroup_api.join(self.host, self.topic, self)

        if self.periodic_enable:    #default=True,
            if self.periodic_fuzzy_delay:   #default=60,
                initial_delay = random.randint(0, self.periodic_fuzzy_delay)
            else:
                initial_delay = None

            self.tg.add_dynamic_timer(self.periodic_tasks,      #增加执行周期任务
                                     initial_delay=initial_delay,
                                     periodic_interval_max=
                                        self.periodic_interval_max)

    #获取属相重定向到manager类
    def __getattr__(self, key):
        manager = self.__dict__.get('manager', None)
        return getattr(manager, key)

    @classmethod    #调用__init__()返回实例，默认参数从CONF获取
    def create(cls, host=None, binary=None, topic=None, manager=None,
               report_interval=None, periodic_enable=None,
               periodic_fuzzy_delay=None, periodic_interval_max=None):
        """Instantiates class and passes back application object.

        :param host: defaults to CONF.host
        :param binary: defaults to basename of executable
        :param topic: defaults to bin_name - 'nova-' part
        :param manager: defaults to CONF.<topic>_manager
        :param report_interval: defaults to CONF.report_interval
        :param periodic_enable: defaults to CONF.periodic_enable
        :param periodic_fuzzy_delay: defaults to CONF.periodic_fuzzy_delay
        :param periodic_interval_max: if set, the max time to wait between runs

        """
        if not host:
            host = CONF.host
        if not binary:
            binary = os.path.basename(sys.argv[0])
        if not topic:
            topic = binary.rpartition('nova-')[2]
        if not manager:
            manager = SERVICE_MANAGERS.get(binary)
        if report_interval is None:
            report_interval = CONF.report_interval
        if periodic_enable is None:
            periodic_enable = CONF.periodic_enable
        if periodic_fuzzy_delay is None:
            periodic_fuzzy_delay = CONF.periodic_fuzzy_delay

        debugger.init()     #debugger ???

        service_obj = cls(host, binary, topic, manager,
                          report_interval=report_interval,
                          periodic_enable=periodic_enable,
                          periodic_fuzzy_delay=periodic_fuzzy_delay,
                          periodic_interval_max=periodic_interval_max)

        return service_obj

    def kill(self):
        """Destroy the service object in the datastore.

        NOTE: Although this method is not used anywhere else than tests, it is
        convenient to have it here, so the tests might easily and in clean way
        stop and remove the service_ref.

        """
        self.stop()
        try:
            self.service_ref.destroy()
        except exception.NotFound:
            LOG.warning(_LW('Service killed that has no database entry'))

    def stop(self):
        try:
            self.rpcserver.stop()
            self.rpcserver.wait()
        except Exception:
            pass

        try:
            self.manager.cleanup_host()
        except Exception:
            LOG.exception(_LE('Service error occurred during cleanup_host'))
            pass

        super(Service, self).stop()

    def periodic_tasks(self, raise_on_error=False):
        """Tasks to be run at a periodic interval."""
        ctxt = context.get_admin_context()
        return self.manager.periodic_tasks(ctxt, raise_on_error=raise_on_error)

    def basic_config_check(self):  #尝试temp文件的创建与删除操作
        """Perform basic config checks before starting processing."""
        # Make sure the tempdir exists and is writable
        try:
            with utils.tempdir():
                pass
        except Exception as e:
            LOG.error(_LE('Temporary directory is invalid: %s'), e)
            sys.exit(1)

    def reset(self):
        self.manager.reset()


class WSGIService(service.Service):
    """Provides ability to launch API from a 'paste' configuration."""

    def __init__(self, name, loader=None, use_ssl=False, max_url_len=None):
        """Initialize, but do not start the WSGI server.

        :param name: The name of the WSGI server given to the loader.
        :param loader: Loads the WSGI application using the given name.
        :returns: None

        """
        self.name = name
        # NOTE(danms): Name can be metadata, osapi_compute, or ec2, per
        # nova.service's enabled_apis
        self.binary = 'nova-%s' % name
        self.topic = None
        self.manager = self._get_manager()
        self.loader = loader or wsgi.Loader()
        self.app = self.loader.load_app(name)
        # inherit all compute_api worker counts from osapi_compute
        if name.startswith('openstack_compute_api'):
            wname = 'osapi_compute'
        else:
            wname = name
        self.host = getattr(CONF, '%s_listen' % name, "0.0.0.0")
        self.port = getattr(CONF, '%s_listen_port' % name, 0)
        self.workers = (getattr(CONF, '%s_workers' % wname, None) or
                        processutils.get_worker_count())
        if self.workers and self.workers < 1:
            worker_name = '%s_workers' % name
            msg = (_("%(worker_name)s value of %(workers)s is invalid, "
                     "must be greater than 0") %
                   {'worker_name': worker_name,
                    'workers': str(self.workers)})
            raise exception.InvalidInput(msg)
        self.use_ssl = use_ssl
        self.server = wsgi.Server(name,
                                  self.app,
                                  host=self.host,
                                  port=self.port,
                                  use_ssl=self.use_ssl,
                                  max_url_len=max_url_len)
        # Pull back actual port used
        self.port = self.server.port
        self.backdoor_port = None
        setup_profiler(name, self.host)

    def reset(self):
        """Reset server greenpool size to default and service version cache.

        :returns: None

        """
        self.server.reset()
        service_obj.Service.clear_min_version_cache()

    def _get_manager(self):
        """Initialize a Manager object appropriate for this service.

        Use the service name to look up a Manager subclass from the
        configuration and initialize an instance. If no class name
        is configured, just return None.

        :returns: a Manager instance, or None.

        """
        manager = SERVICE_MANAGERS.get(self.binary)
        if manager is None:
            return None

        manager_class = importutils.import_class(manager)
        return manager_class()

    def start(self):
        """Start serving this service using loaded configuration.

        Also, retrieve updated port number in case '0' was passed in, which
        indicates a random port should be used.

        :returns: None

        """
        ctxt = context.get_admin_context()
        service_ref = objects.Service.get_by_host_and_binary(ctxt, self.host,
                                                             self.binary)
        if service_ref:
            _update_service_ref(service_ref)
        else:
            try:
                service_ref = _create_service_ref(self, ctxt)
            except (exception.ServiceTopicExists,
                    exception.ServiceBinaryExists):
                # NOTE(danms): If we race to create a record wth a sibling,
                # don't fail here.
                service_ref = objects.Service.get_by_host_and_binary(
                    ctxt, self.host, self.binary)

        if self.manager:
            self.manager.init_host()
            self.manager.pre_start_hook()
            if self.backdoor_port is not None:
                self.manager.backdoor_port = self.backdoor_port
        self.server.start()
        if self.manager:
            self.manager.post_start_hook()

    def stop(self):
        """Stop serving this API.

        :returns: None

        """
        self.server.stop()

    def wait(self):
        """Wait for the service to stop serving this API.

        :returns: None

        """
        self.server.wait()


def process_launcher():
    return service.ProcessLauncher(CONF, restart_method='mutate')


# NOTE(vish): the global launcher is to maintain the existing
#             functionality of calling service.serve +
#             service.wait
_launcher = None


def serve(server, workers=None):
    global _launcher
    if _launcher:
        raise RuntimeError(_('serve() can only be called once'))

    _launcher = service.launch(CONF, server, workers=workers,
                               restart_method='mutate')


def wait():
    _launcher.wait()
