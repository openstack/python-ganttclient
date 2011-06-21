# Copyright (c) 2011 Openstack, LLC.
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

"""
The Zone Aware Scheduler is a base class Scheduler for creating instances
across zones. There are two expansion points to this class for:
1. Assigning Weights to hosts for requested instances
2. Filtering Hosts based on required instance capabilities
"""

import operator
import json

import M2Crypto
import novaclient

from nova import crypto
from nova import db
from nova import exception
from nova import flags
from nova import log as logging
from nova import rpc

from nova.compute import api as compute_api
from nova.scheduler import api
from nova.scheduler import driver

FLAGS = flags.FLAGS
LOG = logging.getLogger('nova.scheduler.zone_aware_scheduler')


class InvalidBlob(exception.NovaException):
    message = _("Ill-formed or incorrectly routed 'blob' data sent "
                "to instance create request.")


class ZoneAwareScheduler(driver.Scheduler):
    """Base class for creating Zone Aware Schedulers."""

    def _call_zone_method(self, context, method, specs, zones):
        """Call novaclient zone method. Broken out for testing."""
        return api.call_zone_method(context, method, specs=specs, zones=zones)

    def _provision_resource_locally(self, context, build_plan_item,
                                    request_spec, kwargs):
        """Create the requested resource in this Zone."""
        host = build_plan_item['hostname']
        base_options = request_spec['instance_properties']

        # TODO(sandy): I guess someone needs to add block_device_mapping
        # support at some point? Also, OS API has no concept of security
        # groups.
        instance = compute_api.API().create_db_entry_for_new_instance(context,
            base_options, None, [])

        instance_id = instance['id']
        kwargs['instance_id'] = instance_id

        rpc.cast(context,
                 db.queue_get_for(context, "compute", host),
                 {"method": "run_instance",
                  "args": kwargs})
        LOG.debug(_("Provisioning locally via compute node %(host)s")
                            % locals())

    def _decrypt_blob(self, blob):
        """Returns the decrypted blob or None if invalid. Broken out
        for testing."""
        decryptor = crypto.decryptor(FLAGS.build_plan_encryption_key)
        try:
            json_entry = decryptor(blob)
            return json.dumps(entry)
        except M2Crypto.EVP.EVPError:
            pass
        return None

    def _ask_child_zone_to_create_instance(self, context, zone_info,
                                           request_spec, kwargs):
        """Once we have determined that the request should go to one
        of our children, we need to fabricate a new POST /servers/
        call with the same parameters that were passed into us.

        Note that we have to reverse engineer from our args to get back the
        image, flavor, ipgroup, etc. since the original call could have
        come in from EC2 (which doesn't use these things)."""

        instance_type = request_spec['instance_type']
        instance_properties = request_spec['instance_properties']

        name = instance_properties['display_name']
        image_ref = instance_properties['image_ref']
        meta = instance_properties['metadata']
        flavor_id = instance_type['flavorid']
        reservation_id = instance_properties['reservation_id']

        files = kwargs['injected_files']
        ipgroup = None  # Not supported in OS API ... yet

        child_zone = zone_info['child_zone']
        child_blob = zone_info['child_blob']
        zone = db.zone_get(context, child_zone)
        url = zone.api_url
        LOG.debug(_("Forwarding instance create call to child zone %(url)s"
                    ". ReservationID=%(reservation_id)s")
                    % locals())
        nova = None
        try:
            nova = novaclient.OpenStack(zone.username, zone.password, None,
                                        url)
            nova.authenticate()
        except novaclient.exceptions.BadRequest, e:
            raise exception.NotAuthorized(_("Bad credentials attempting "
                            "to talk to zone at %(url)s.") % locals())

        nova.servers.create(name, image_ref, flavor_id, ipgroup, meta, files,
                            child_blob, reservation_id=reservation_id)

    def _provision_resource_from_blob(self, context, build_plan_item,
                                      instance_id, request_spec, kwargs):
        """Create the requested resource locally or in a child zone
           based on what is stored in the zone blob info.

           Attempt to decrypt the blob to see if this request is:
           1. valid, and
           2. intended for this zone or a child zone.

           Note: If we have "blob" that means the request was passed
           into us from a parent zone. If we have "child_blob" that
           means we gathered the info from one of our children.
           It's possible that, when we decrypt the 'blob' field, it
           contains "child_blob" data. In which case we forward the
           request."""

        host_info = None
        if "blob" in build_plan_item:
            # Request was passed in from above. Is it for us?
            host_info = self._decrypt_blob(build_plan_item['blob'])
        elif "child_blob" in build_plan_item:
            # Our immediate child zone provided this info ...
            host_info = build_plan_item

        if not host_info:
            raise InvalidBlob()

        # Valid data ... is it for us?
        if 'child_zone' in host_info and 'child_blob' in host_info:
            self._ask_child_zone_to_create_instance(context, host_info,
                                                    request_spec, kwargs)
        else:
            self._provision_resource_locally(context, host_info, request_spec,
                                             kwargs)

    def _provision_resource(self, context, build_plan_item, instance_id,
                            request_spec, kwargs):
        """Create the requested resource in this Zone or a child zone."""
        if "hostname" in build_plan_item:
            self._provision_resource_locally(context, build_plan_item,
                                             request_spec, kwargs)
            return

        self._provision_resource_from_blob(context, build_plan_item,
                                           instance_id, request_spec, kwargs)

    def _adjust_child_weights(self, child_results, zones):
        """Apply the Scale and Offset values from the Zone definition
        to adjust the weights returned from the child zones. Alters
        child_results in place.
        """
        for zone, result in child_results:
            if not result:
                continue
            
            for zone_rec in zones:
                if zone_rec['api_url'] != zone:
                    continue

                for item in result:
                    try:
                        offset = zone_rec['weight_offset']
                        scale = zone_rec['weight_scale']
                        raw_weight = item['weight']
                        cooked_weight = offset + scale * raw_weight
                        item['weight'] = cooked_weight
                        item['raw_weight'] = raw_weight
                    except Exception, e:
                        LOG.exception(_("Bad child zone scaling values "
                                        "for Zone: %(zone)s") % locals())

    def schedule_run_instance(self, context, instance_id, request_spec,
                              *args, **kwargs):
        """This method is called from nova.compute.api to provision
        an instance. However we need to look at the parameters being
        passed in to see if this is a request to:
        1. Create a Build Plan and then provision, or
        2. Use the Build Plan information in the request parameters
           to simply create the instance (either in this zone or
           a child zone).
        """

        # TODO(sandy): We'll have to look for richer specs at some point.

        blob = request_spec.get('blob')
        if blob:
            self._provision_resource(context, request_spec, instance_id,
                                    request_spec, kwargs)
            return None

        # Create build plan and provision ...
        build_plan = self.select(context, request_spec)
        if not build_plan:
            raise driver.NoValidHost(_('No hosts were available'))

        for num in xrange(request_spec['num_instances']):
            if not build_plan:
                break

            build_plan_item = build_plan.pop(0)
            self._provision_resource(context, build_plan_item, instance_id,
                                     request_spec, kwargs)

        # Returning None short-circuits the routing to Compute (since
        # we've already done it here)
        return None

    def select(self, context, request_spec, *args, **kwargs):
        """Select returns a list of weights and zone/host information
        corresponding to the best hosts to service the request. Any
        child zone information has been encrypted so as not to reveal
        anything about the children.
        """
        return self._schedule(context, "compute", request_spec,
                              *args, **kwargs)

    # TODO(sandy): We're only focused on compute instances right now,
    # so we don't implement the default "schedule()" method required
    # of Schedulers.
    def schedule(self, context, topic, request_spec, *args, **kwargs):
        """The schedule() contract requires we return the one
        best-suited host for this request.
        """
        raise driver.NoValidHost(_('No hosts were available'))

    def _schedule(self, context, topic, request_spec, *args, **kwargs):
        """Returns a list of hosts that meet the required specs,
        ordered by their fitness.
        """

        if topic != "compute":
            raise NotImplemented(_("Zone Aware Scheduler only understands "
                                   "Compute nodes (for now)"))

        #TODO(sandy): how to infer this from OS API params?
        num_instances = 1

        # Filter local hosts based on requirements ...
        host_list = self.filter_hosts(num_instances, request_spec)

        # TODO(sirp): weigh_hosts should also be a function of 'topic' or
        # resources, so that we can apply different objective functions to it

        # then weigh the selected hosts.
        # weighted = [{weight=weight, name=hostname}, ...]
        weighted = self.weigh_hosts(num_instances, request_spec, host_list)

        # Next, tack on the best weights from the child zones ...
        json_spec = json.dumps(request_spec)
        all_zones = db.zone_get_all(context)
        child_results = self._call_zone_method(context, "select",
                specs=json_spec, zones=all_zones)
        self._adjust_child_weights(child_results, all_zones)
        for child_zone, result in child_results:
            for weighting in result:
                # Remember the child_zone so we can get back to
                # it later if needed. This implicitly builds a zone
                # path structure.
                host_dict = {"weight": weighting["weight"],
                             "child_zone": child_zone,
                             "child_blob": weighting["blob"]}
                weighted.append(host_dict)

        weighted.sort(key=operator.itemgetter('weight'))
        return weighted

    def filter_hosts(self, num, request_spec):
        """Derived classes must override this method and return
           a list of hosts in [(hostname, capability_dict)] format.
        """
        # NOTE(sirp): The default logic is the equivalent to AllHostsFilter
        service_states = self.zone_manager.service_states
        return [(host, services)
                for host, services in service_states.iteritems()]

    def weigh_hosts(self, num, request_spec, hosts):
        """Derived classes may override this to provide more sophisticated
        scheduling objectives
        """
        # NOTE(sirp): The default logic is the same as the NoopCostFunction
        return [dict(weight=1, hostname=host) for host, caps in hosts]
