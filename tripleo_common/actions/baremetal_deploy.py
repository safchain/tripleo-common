# Copyright 2018 Red Hat, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import logging

import jsonschema
import metalsmith
from metalsmith import sources
from mistral_lib import actions
from openstack import exceptions as sdk_exc
import six

from tripleo_common.actions import base
from tripleo_common.utils import keystone

LOG = logging.getLogger(__name__)


def _provisioner(context):
    session = keystone.get_session(context)
    return metalsmith.Provisioner(session=session)


_INSTANCES_INPUT_SCHEMA = {
    'type': 'array',
    'items': {
        'type': 'object',
        'properties': {
            'capabilities': {'type': 'object'},
            'hostname': {'type': 'string',
                         'minLength': 2,
                         'maxLength': 255},
            'image': {'type': 'string'},
            'image_checksum': {'type': 'string'},
            'image_kernel': {'type': 'string'},
            'image_ramdisk': {'type': 'string'},
            'name': {'type': 'string'},
            'nics': {'type': 'array',
                     'items': {'type': 'object',
                               'properties': {
                                   'network': {'type': 'string'},
                                   'port': {'type': 'string'},
                                   'fixed_ip': {'type': 'string'},
                                   'subnet': {'type': 'string'},
                               },
                               'additionalProperties': False}},
            'profile': {'type': 'string'},
            'resource_class': {'type': 'string'},
            'root_size_gb': {'type': 'integer', 'minimum': 4},
            'swap_size_mb': {'type': 'integer', 'minimum': 64},
            'traits': {'type': 'array',
                       'items': {'type': 'string'}},
        },
        'additionalProperties': False,
        # Host name is required, but defaults to name in _validate_instances
        'required': ['hostname'],
    }
}
"""JSON schema of the input for these actions."""


class CheckExistingInstancesAction(base.TripleOAction):
    """Detect which requested instances have already been provisioned."""

    def __init__(self, instances):
        super(CheckExistingInstancesAction, self).__init__()
        self.instances = instances

    def run(self, context):
        try:
            _validate_instances(self.instances)
        except Exception as exc:
            LOG.error('Failed to validate provided instances. %s', exc)
            return actions.Result(error=six.text_type(exc))

        provisioner = _provisioner(context)

        not_found = []
        found = []
        for request in self.instances:
            try:
                instance = provisioner.show_instance(request['hostname'])
            # TODO(dtantsur): replace Error with a specific exception
            except (sdk_exc.ResourceNotFound, metalsmith.exceptions.Error):
                not_found.append(request)
            except Exception as exc:
                message = ('Failed to request instance information for '
                           'hostname %s' % request['hostname'])
                LOG.exception(message)
                return actions.Result(
                    error="%s. %s: %s" % (message, type(exc).__name__, exc)
                )
            else:
                # NOTE(dtantsur): metalsmith can match instances by node names,
                # provide a safeguard to avoid conflicts.
                if (instance.hostname and
                        instance.hostname != request['hostname']):
                    error = ("Requested hostname %s was not found, but the "
                             "deployed node %s has a matching name. Refusing "
                             "to proceed to avoid confusing results. Please "
                             "either rename the node or use a different "
                             "hostname") % (request['hostname'], instance.uuid)
                    return actions.Result(error=error)

                found.append(instance.to_dict())

        if found:
            LOG.info('Found existing instances: %s',
                     ', '.join('%s (on node %s)' % (i['hostname'], i['uuid'])
                               for i in found))
        if not_found:
            LOG.info('Instance(s) %s do not exist',
                     ', '.join(r['hostname'] for r in not_found))

        return {
            'not_found': not_found,
            'instances': found
        }


class ReserveNodesAction(base.TripleOAction):
    """Reserve nodes for requested instances."""

    def __init__(self, instances, default_resource_class='baremetal'):
        super(ReserveNodesAction, self).__init__()
        self.instances = instances
        self.default_resource_class = default_resource_class

    def run(self, context):
        try:
            _validate_instances(self.instances)
        except Exception as exc:
            LOG.error('Failed to validate provided instances. %s', exc)
            return actions.Result(error=six.text_type(exc))

        provisioner = _provisioner(context)

        # TODO(dtantsur): looping over instances is not very optimal, change it
        # to metalsmith plan deployment API when it's available.
        result = []
        nodes = []
        try:
            for instance in self.instances:
                LOG.debug('Trying to reserve a node for instance %s', instance)
                if instance.get('name'):
                    # NOTE(dtantsur): metalsmith accepts list of nodes to pick
                    # from. We implement a simplest case when a user can pick a
                    # node by its name (actually, UUID will also work).
                    candidates = [instance['name']]
                else:
                    candidates = None

                if instance.get('profile'):
                    # TODO(dtantsur): change to traits?
                    instance.setdefault(
                        'capabilities', {})['profile'] = instance['profile']

                node = provisioner.reserve_node(
                    resource_class=instance.get('resource_class') or
                    self.default_resource_class,
                    capabilities=instance.get('capabilities'),
                    candidates=candidates,
                    traits=instance.get('traits'))
                LOG.info('Reserved node %s for instance %s', node, instance)
                nodes.append(node)
                result.append({'node': node.id, 'instance': instance})
        except Exception as exc:
            LOG.exception('Provisioning failed, cleaning up')
            # Remove all reservations on failure
            _release_nodes(provisioner, nodes)
            return actions.Result(
                error="%s: %s" % (type(exc).__name__, exc)
            )

        return {'reservations': result}


class DeployNodeAction(base.TripleOAction):
    """Provision instance on a previously reserved node."""

    def __init__(self, instance, node, ssh_keys=None,
                 # For compatibility with deployment based on heat+nova
                 ssh_user_name='heat-admin',
                 default_image='overcloud-full',
                 default_network='ctlplane',
                 # 50 is the default for old flavors, subtracting 1G to account
                 # for partitioning and configdrive.
                 default_root_size=49):
        super(DeployNodeAction, self).__init__()
        self.instance = instance
        self.node = node
        self.config = metalsmith.InstanceConfig(ssh_keys=ssh_keys)
        self.config.add_user(ssh_user_name, admin=True, sudo=True)
        self.default_image = default_image
        self.default_network = default_network
        self.default_root_size = default_root_size

    def run(self, context):
        try:
            _validate_instances([self.instance],
                                default_image=self.default_image)
        except Exception as exc:
            LOG.error('Failed to validate the request. %s', exc)
            return actions.Result(error=six.text_type(exc))

        provisioner = _provisioner(context)

        LOG.debug('Starting provisioning of %s on node %s',
                  self.instance, self.node)
        try:
            image = _get_source(self.instance)
            instance = provisioner.provision_node(
                self.node,
                config=self.config,
                hostname=self.instance['hostname'],
                image=image,
                nics=self.instance.get('nics',
                                       [{'network': self.default_network}]),
                root_size_gb=self.instance.get('root_size_gb',
                                               self.default_root_size),
                swap_size_mb=self.instance.get('swap_size_mb'),
            )
        except Exception as exc:
            LOG.exception('Provisioning of %s on node %s failed',
                          self.instance, self.node)
            _release_nodes(provisioner, [self.node])
            return actions.Result(
                error="%s: %s" % (type(exc).__name__, exc)
            )

        LOG.info('Started provisioning of %s on node %s',
                 self.instance, self.node)
        return instance.to_dict()


class WaitForDeploymentAction(base.TripleOAction):
    """Wait for the instance to be deployed."""

    def __init__(self, instance, timeout=3600):
        super(WaitForDeploymentAction, self).__init__()
        self.instance = instance
        self.timeout = timeout

    def run(self, context):
        provisioner = _provisioner(context)

        LOG.debug('Waiting for instance %s to provision',
                  self.instance['hostname'])
        try:
            instance = provisioner.wait_for_provisioning(
                [self.instance['uuid']], timeout=self.timeout)[0]
        except Exception as exc:
            LOG.exception('Provisioning of instance %s failed or timed out',
                          self.instance['hostname'])
            # Do not tear down, leave it up for the calling code to handle.
            return actions.Result(
                error="%s: %s" % (type(exc).__name__, exc)
            )
        LOG.info('Successfully provisioned instance %s',
                 self.instance['hostname'])
        return instance.to_dict()


class UndeployInstanceAction(base.TripleOAction):
    """Undeploy a previously deployed instance."""

    def __init__(self, instance, timeout=1800):
        super(UndeployInstanceAction, self).__init__()
        self.instance = instance
        self.timeout = timeout

    def run(self, context):
        provisioner = _provisioner(context)

        try:
            instance = provisioner.show_instance(self.instance)
        except Exception:
            LOG.warning('Cannot get instance %s, assuming already deleted',
                        self.instance)
            return

        LOG.debug('Unprovisioning instance %s', instance.hostname)
        provisioner.unprovision_node(instance.node, wait=self.timeout)
        LOG.info('Successfully unprovisioned %s', instance.hostname)


def _validate_instances(instances, default_image='overcloud-full'):
    for inst in instances:
        if inst.get('name') and not inst.get('hostname'):
            inst['hostname'] = inst['name']

        # Set the default image so that the source validation can succeed.
        inst.setdefault('image', default_image)

    jsonschema.validate(instances, _INSTANCES_INPUT_SCHEMA)

    hostnames = set()
    names = set()
    for inst in instances:
        # NOTE(dtantsur): validate image parameters
        _get_source(inst)

        if inst['hostname'] in hostnames:
            raise ValueError('Hostname %s is used more than once' %
                             inst['hostname'])
        hostnames.add(inst['hostname'])

        if inst.get('name'):
            if inst['name'] in names:
                raise ValueError('Node %s is requested more than once' %
                                 inst['name'])
            names.add(inst['name'])


def _release_nodes(provisioner, nodes):
    for node in nodes:
        LOG.debug('Removing reservation from node %s', node)
        try:
            provisioner.unprovision_node(node)
        except Exception:
            LOG.exception('Unable to release node %s, moving on', node)
        else:
            LOG.info('Removed reservation from node %s', node)


def _get_source(instance):
    return sources.detect(image=instance.get('image'),
                          kernel=instance.get('image_kernel'),
                          ramdisk=instance.get('image_ramdisk'),
                          checksum=instance.get('image_checksum'))
