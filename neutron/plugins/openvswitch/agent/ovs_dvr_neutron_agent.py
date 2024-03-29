# Copyright 2014, Hewlett Packard, Inc.
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
# @author: Vivekanandan Narasimhan, Hewlett-Packard Inc


from neutron.api.rpc import dvr_rpc
from neutron.common import constants as q_const
from neutron.openstack.common import log as logging
from neutron.plugins.openvswitch.common import constants


LOG = logging.getLogger(__name__)


# A class to represent a DVR-hosted subnet including vif_ports resident on
# that subnet
class LocalDVRSubnetMapping:
    def __init__(self, subnet, csnat_ofport=constants.OFPORT_INVALID):
        # set of commpute ports on on this dvr subnet
        self.compute_ports = {}
        self.subnet = subnet
        self.csnat_ofport = csnat_ofport
        self.dvr_owned = False

    def __str__(self):
        return ("subnet = %s compute_ports = %s csnat_port = %s"
                " is_dvr_owned = %s" %
                (self.subnet, self.get_compute_ofports(),
                 self.get_csnat_ofport(), self.is_dvr_owned()))

    def get_subnet_info(self):
        return self.subnet

    def set_dvr_owned(self, owned):
        self.dvr_owned = owned

    def is_dvr_owned(self):
        return self.dvr_owned

    def add_compute_ofport(self, vif_id, ofport):
        self.compute_ports[vif_id] = ofport

    def remove_compute_ofport(self, vif_id):
        self.compute_ports.pop(vif_id, 0)

    def remove_all_compute_ofports(self):
        self.compute_ports.clear()

    def get_compute_ofports(self):
        return self.compute_ports

    def set_csnat_ofport(self, ofport):
        self.csnat_ofport = ofport

    def get_csnat_ofport(self):
        return self.csnat_ofport


class OVSPort:
    def __init__(self, id, ofport, mac, device_owner):
        self.id = id
        self.mac = mac
        self.ofport = ofport
        self.subnets = set()
        self.device_owner = device_owner

    def __str__(self):
        return ("OVSPort: id = %s, ofport = %s, mac = %s,"
                "device_owner = %s, subnets = %s" %
                (self.id, self.ofport, self.mac,
                 self.device_owner, self.subnets))

    def add_subnet(self, subnet_id):
        self.subnets.add(subnet_id)

    def remove_subnet(self, subnet_id):
        self.subnets.remove(subnet_id)

    def remove_all_subnets(self):
        self.subnets.clear()

    def get_subnets(self):
        return self.subnets

    def get_device_owner(self):
        return self.device_owner

    def get_mac(self):
        return self.mac

    def get_ofport(self):
        return self.ofport


class OVSDVRNeutronAgent(dvr_rpc.DVRAgentRpcApiMixin):
    '''Implements OVS-based DVR(Distributed Virtual Router), for
    overlay networks.

    '''

    # history
    #   1.0 Initial version

    def __init__(self, context, plugin_rpc, integ_br, tun_br,
                 patch_int_ofport=constants.OFPORT_INVALID,
                 patch_tun_ofport=constants.OFPORT_INVALID,
                 host=None, enable_tunneling=False,
                 enable_distributed_routing=False):
        self.context = context
        self.plugin_rpc = plugin_rpc
        self.int_br = integ_br
        self.tun_br = tun_br
        self.patch_int_ofport = patch_int_ofport
        self.patch_tun_ofport = patch_tun_ofport
        self.host = host
        self.enable_tunneling = enable_tunneling
        self.enable_distributed_routing = enable_distributed_routing

    def reset_ovs_parameters(self, integ_br, tun_br,
                             patch_int_ofport, patch_tun_ofport):
        '''Reset the openvswitch parameters
        '''
        if not self.enable_tunneling:
            return

        if not self.enable_distributed_routing:
            return

        self.int_br = integ_br
        self.tun_br = tun_br
        self.patch_int_ofport = patch_int_ofport
        self.patch_tun_ofport = patch_tun_ofport

    def setup_dvr_flows_on_integ_tun_br(self):
        '''Setup up initial dvr flows into integration bridge and tunnel
        bridge.
        '''
        if not self.enable_tunneling:
            return

        if not self.enable_distributed_routing:
            return

        # get the local DVR MAC Address
        try:
            LOG.debug("L2 Agent operating in DVR Mode")
            self.dvr_mac_address = None
            self.local_dvr_map = {}
            self.local_csnat_map = {}
            self.local_ports = {}
            self.registered_dvr_macs = set()
            details = self.plugin_rpc.get_dvr_mac_address_by_host(
                self.context, self.host)
            LOG.debug("L2 Agent DVR: Received response for "
                      "get_dvr_mac_address_by_host() from "
                      "plugin: %r", details)
            self.dvr_mac_address = details['mac_address']
        except Exception:
            LOG.exception(_("DVR: Failed to obtain local DVR Mac address"))
            self.enable_distributed_routing = False
            # switch all traffic using L2 learning
            self.int_br.add_flow(table=constants.LOCAL_SWITCHING,
                                 priority=1, actions="normal")
            return

        # Remove existing flows in integration bridge
        self.int_br.remove_all_flows()

        # Add a canary flow to int_br to track OVS restarts
        self.int_br.add_flow(table=constants.CANARY_TABLE, priority=0,
                             actions="drop")

        # Insert 'drop' action as the default for Table DVR_TO_SRC_MAC
        self.int_br.add_flow(table=constants.DVR_TO_SRC_MAC,
                             priority=1,
                             actions="drop")

        # Insert 'normal' action as the default for Table LOCAL_SWITCHING
        self.int_br.add_flow(table=constants.LOCAL_SWITCHING,
                             priority=1,
                             actions="normal")

        dvr_macs = self.plugin_rpc.get_dvr_mac_address_list(self.context)
        LOG.debug("L2 Agent DVR: Received these MACs: %r", dvr_macs)
        for mac in dvr_macs:
            if mac['mac_address'] == self.dvr_mac_address:
                continue
            # Table 0 (default) will now sort DVR traffic from other
            # traffic depending on in_port
            self.int_br.add_flow(table=constants.LOCAL_SWITCHING,
                                 priority=2,
                                 in_port=self.patch_tun_ofport,
                                 dl_src=mac['mac_address'],
                                 actions="resubmit(,%s)" %
                                 constants.DVR_TO_SRC_MAC)
            # Table DVR_NOT_LEARN ensures unique dvr macs in the cloud
            # are not learnt, as they may
            # result in flow explosions
            self.tun_br.add_flow(table=constants.DVR_NOT_LEARN,
                                 priority=1,
                                 dl_src=mac['mac_address'],
                                 actions="output:%s" % self.patch_int_ofport)

            self.registered_dvr_macs.add(mac['mac_address'])

        self.tun_br.add_flow(priority=1,
                             in_port=self.patch_int_ofport,
                             actions="resubmit(,%s)" %
                             constants.DVR_PROCESS)
        # table-miss should be sent to learning table
        self.tun_br.add_flow(table=constants.DVR_NOT_LEARN,
                             priority=0,
                             actions="resubmit(,%s)" %
                             constants.LEARN_FROM_TUN)

        self.tun_br.add_flow(table=constants.DVR_PROCESS,
                             priority=0,
                             actions="resubmit(,%s)" %
                             constants.PATCH_LV_TO_TUN)

    def dvr_mac_address_update(self, dvr_macs):
        if not self.enable_tunneling:
            return

        if not self.enable_distributed_routing:
            return

        LOG.debug("DVR Mac address update with host-mac: %s", dvr_macs)

        if not self.dvr_mac_address:
            LOG.debug("Self mac unknown, ignoring this"
                      " dvr_mac_address_update() ")
            return

        dvr_host_macs = set()
        for entry in dvr_macs:
            if entry['mac_address'] == self.dvr_mac_address:
                continue
            dvr_host_macs.add(entry['mac_address'])

        if dvr_host_macs == self.registered_dvr_macs:
            LOG.debug("DVR Mac address already up to date")
            return

        dvr_macs_added = dvr_host_macs - self.registered_dvr_macs
        dvr_macs_removed = self.registered_dvr_macs - dvr_host_macs

        for oldmac in dvr_macs_removed:
            self.int_br.delete_flows(table=constants.LOCAL_SWITCHING,
                                     in_port=self.patch_tun_ofport,
                                     dl_src=oldmac)
            self.tun_br.delete_flows(table=constants.DVR_NOT_LEARN,
                                     dl_src=oldmac)
            LOG.debug("Removed DVR MAC flow for %s", oldmac)
            self.registered_dvr_macs.remove(oldmac)

        for newmac in dvr_macs_added:
            self.int_br.add_flow(table=constants.LOCAL_SWITCHING,
                                 priority=2,
                                 in_port=self.patch_tun_ofport,
                                 dl_src=newmac,
                                 actions="resubmit(,%s)" %
                                 constants.DVR_TO_SRC_MAC)
            self.tun_br.add_flow(table=constants.DVR_NOT_LEARN,
                                 priority=1,
                                 dl_src=newmac,
                                 actions="output:%s" % self.patch_int_ofport)
            LOG.debug("Added DVR MAC flow for %s", newmac)
            self.registered_dvr_macs.add(newmac)

    def is_dvr_router_interface(self, device_owner):
        return device_owner == q_const.DEVICE_OWNER_DVR_INTERFACE

    def process_tunneled_network(self, network_type, lvid, segmentation_id):
        if not self.enable_tunneling:
            return
        if not self.enable_distributed_routing:
            return
        self.tun_br.add_flow(table=constants.TUN_TABLE[network_type],
                             priority=1,
                             tun_id=segmentation_id,
                             actions="mod_vlan_vid:%s,"
                             "resubmit(,%s)" %
                             (lvid, constants.DVR_NOT_LEARN))

    def _bind_distributed_router_interface_port(self, port, fixed_ips,
                                                device_owner, local_vlan):
        # since router port must have only one fixed IP, directly
        # use fixed_ips[0]
        subnet_uuid = fixed_ips[0]['subnet_id']
        csnat_ofport = constants.OFPORT_INVALID
        ldm = None
        if subnet_uuid in self.local_dvr_map:
            ldm = self.local_dvr_map[subnet_uuid]
            csnat_ofport = ldm.get_csnat_ofport()
            if csnat_ofport == constants.OFPORT_INVALID:
                LOG.error(_("DVR: Duplicate DVR router interface detected "
                          "for subnet %s"), subnet_uuid)
                return
        else:
            # set up LocalDVRSubnetMapping available for this subnet
            subnet_info = self.plugin_rpc.get_subnet_for_dvr(self.context,
                                                             subnet_uuid)
            if not subnet_info:
                LOG.error(_("DVR: Unable to retrieve subnet information"
                          " for subnet_id %s"), subnet_uuid)
                return
            LOG.debug("get_subnet_for_dvr for subnet %s returned with %s" %
                      (subnet_uuid, subnet_info))
            ldm = LocalDVRSubnetMapping(subnet_info)
            self.local_dvr_map[subnet_uuid] = ldm

        # DVR takes over
        ldm.set_dvr_owned(True)

        subnet_info = ldm.get_subnet_info()
        ip_subnet = subnet_info['cidr']
        local_compute_ports = (
            self.plugin_rpc.get_compute_ports_on_host_by_subnet(
                self.context, self.host, subnet_uuid))
        LOG.debug("DVR: List of ports received from "
                  "get_compute_ports_on_host_by_subnet %r",
                  local_compute_ports)
        for prt in local_compute_ports:
            vif = self.int_br.get_vif_port_by_id(prt['id'])
            if not vif:
                continue
            ldm.add_compute_ofport(vif.vif_id, vif.ofport)
            if vif.vif_id in self.local_ports:
                # ensure if a compute port is already on
                # a different dvr routed subnet
                # if yes, queue this subnet to that port
                ovsport = self.local_ports[vif.vif_id]
                ovsport.add_subnet(subnet_uuid)
            else:
                # the compute port is discovered first here that its on
                # a dvr routed subnet queue this subnet to that port
                ovsport = OVSPort(vif.vif_id, vif.ofport,
                                  vif.vif_mac, prt['device_owner'])

                ovsport.add_subnet(subnet_uuid)
                self.local_ports[vif.vif_id] = ovsport

            # create rule for just this vm port
            self.int_br.add_flow(table=constants.DVR_TO_SRC_MAC,
                                 priority=4,
                                 dl_vlan=local_vlan,
                                 dl_dst=ovsport.get_mac(),
                                 actions="strip_vlan,mod_dl_src:%s,"
                                 "output:%s" %
                                 (subnet_info['gateway_mac'],
                                  ovsport.get_ofport()))

        # create rule to forward broadcast/multicast frames from dvr
        # router interface to appropriate local tenant ports
        ofports = ','.join(map(str, ldm.get_compute_ofports().values()))
        if csnat_ofport != constants.OFPORT_INVALID:
            ofports = str(csnat_ofport) + ',' + ofports
        if ofports:
            self.int_br.add_flow(table=constants.DVR_TO_SRC_MAC,
                                 priority=2,
                                 proto='ip',
                                 dl_vlan=local_vlan,
                                 nw_dst=ip_subnet,
                                 actions="strip_vlan,mod_dl_src:%s,"
                                 "output:%s" %
                                 (subnet_info['gateway_mac'], ofports))

        self.tun_br.add_flow(table=constants.DVR_PROCESS,
                             priority=3,
                             dl_vlan=local_vlan,
                             proto='arp',
                             nw_dst=subnet_info['gateway_ip'],
                             actions="drop")

        self.tun_br.add_flow(table=constants.DVR_PROCESS,
                             priority=2,
                             dl_vlan=local_vlan,
                             dl_dst=port.vif_mac,
                             actions="drop")

        self.tun_br.add_flow(table=constants.DVR_PROCESS,
                             priority=1,
                             dl_vlan=local_vlan,
                             dl_src=port.vif_mac,
                             actions="mod_dl_src:%s,resubmit(,%s)" %
                             (self.dvr_mac_address,
                              constants.PATCH_LV_TO_TUN))

        # the dvr router interface is itself a port, so capture it
        # queue this subnet to that port. A subnet appears only once as
        # a router interface on any given router
        ovsport = OVSPort(port.vif_id, port.ofport,
                          port.vif_mac, device_owner)
        ovsport.add_subnet(subnet_uuid)
        self.local_ports[port.vif_id] = ovsport

    def _bind_compute_port_on_dvr_subnet(self, port, fixed_ips,
                                         device_owner, local_vlan):
        # Handle new compute port added use-case
        subnet_uuid = None
        for ips in fixed_ips:
            if ips['subnet_id'] not in self.local_dvr_map:
                continue
            subnet_uuid = ips['subnet_id']
            ldm = self.local_dvr_map[subnet_uuid]
            if not ldm.is_dvr_owned():
                # well this is csnat stuff, let dvr come in
                # and do plumbing for this vm later
                continue

            # This confirms that this compute port belongs
            # to a dvr hosted subnet.
            # Accomodate this VM Port into the existing rule in
            # the integration bridge
            LOG.debug("DVR: Plumbing compute port %s", port.vif_id)
            subnet_info = ldm.get_subnet_info()
            ip_subnet = subnet_info['cidr']
            csnat_ofport = ldm.get_csnat_ofport()
            ldm.add_compute_ofport(port.vif_id, port.ofport)
            if port.vif_id in self.local_ports:
                # ensure if a compute port is already on a different
                # dvr routed subnet
                # if yes, queue this subnet to that port
                ovsport = self.local_ports[port.vif_id]
                ovsport.add_subnet(subnet_uuid)
            else:
                # the compute port is discovered first here that its
                # on a dvr routed subnet, queue this subnet to that port
                ovsport = OVSPort(port.vif_id, port.ofport,
                                  port.vif_mac, device_owner)

                ovsport.add_subnet(subnet_uuid)
                self.local_ports[port.vif_id] = ovsport
            # create a rule for this vm port
            self.int_br.add_flow(table=constants.DVR_TO_SRC_MAC,
                                 priority=4,
                                 dl_vlan=local_vlan,
                                 dl_dst=ovsport.get_mac(),
                                 actions="strip_vlan,mod_dl_src:%s,"
                                 "output:%s" %
                                 (subnet_info['gateway_mac'],
                                  ovsport.get_ofport()))
            ofports = ','.join(map(str, ldm.get_compute_ofports().values()))

            if csnat_ofport != constants.OFPORT_INVALID:
                ofports = str(csnat_ofport) + ',' + ofports
            self.int_br.add_flow(table=constants.DVR_TO_SRC_MAC,
                                 priority=2,
                                 proto='ip',
                                 dl_vlan=local_vlan,
                                 nw_dst=ip_subnet,
                                 actions="strip_vlan,mod_dl_src:%s,"
                                 " output:%s" %
                                 (subnet_info['gateway_mac'], ofports))

    def _bind_centralized_snat_port_on_dvr_subnet(self, port, fixed_ips,
                                                  device_owner, local_vlan):
        if port.vif_id in self.local_ports:
            # throw an error if CSNAT port is already on a different
            # dvr routed subnet
            ovsport = self.local_ports[port.vif_id]
            subs = list(ovsport.get_subnets())
            LOG.error(_("Centralized-SNAT port %s already seen on "),
                      port.vif_id)
            LOG.error(_("a different subnet %s"), subs[0])
            return
        # since centralized-SNAT (CSNAT) port must have only one fixed
        # IP, directly use fixed_ips[0]
        subnet_uuid = fixed_ips[0]['subnet_id']
        ldm = None
        subnet_info = None
        if subnet_uuid not in self.local_dvr_map:
            # no csnat ports seen on this subnet - create csnat state
            # for this subnet
            subnet_info = self.plugin_rpc.get_subnet_for_dvr(self.context,
                                                             subnet_uuid)
            ldm = LocalDVRSubnetMapping(subnet_info, port.ofport)
            self.local_dvr_map[subnet_uuid] = ldm
        else:
            ldm = self.local_dvr_map[subnet_uuid]
            subnet_info = ldm.get_subnet_info()
            # Store csnat OF Port in the existing DVRSubnetMap
            ldm.set_csnat_ofport(port.ofport)

        # create ovsPort footprint for csnat port
        ovsport = OVSPort(port.vif_id, port.ofport,
                          port.vif_mac, device_owner)
        ovsport.add_subnet(subnet_uuid)
        self.local_ports[port.vif_id] = ovsport

        self.int_br.add_flow(table=constants.DVR_TO_SRC_MAC,
                             priority=4,
                             dl_vlan=local_vlan,
                             dl_dst=ovsport.get_mac(),
                             actions="strip_vlan,mod_dl_src:%s,"
                             " output:%s" %
                             (subnet_info['gateway_mac'],
                              ovsport.get_ofport()))
        ofports = ','.join(map(str, ldm.get_compute_ofports().values()))
        ofports = str(ldm.get_csnat_ofport()) + ',' + ofports
        ip_subnet = subnet_info['cidr']
        self.int_br.add_flow(table=constants.DVR_TO_SRC_MAC,
                             priority=2,
                             proto='ip',
                             dl_vlan=local_vlan,
                             nw_dst=ip_subnet,
                             actions="strip_vlan,mod_dl_src:%s,"
                             " output:%s" %
                             (subnet_info['gateway_mac'], ofports))

    def bind_port_to_dvr(self, port, network_type, fixed_ips,
                         device_owner, local_vlan_id):
        # a port coming up as distributed router interface
        if not self.enable_tunneling:
            return

        if not self.enable_distributed_routing:
            return

        if network_type not in constants.TUNNEL_NETWORK_TYPES:
            return

        if device_owner == q_const.DEVICE_OWNER_DVR_INTERFACE:
            self._bind_distributed_router_interface_port(port, fixed_ips,
                                                         device_owner,
                                                         local_vlan_id)

        if device_owner and device_owner.startswith('compute:'):
            self._bind_compute_port_on_dvr_subnet(port, fixed_ips,
                                                  device_owner,
                                                  local_vlan_id)

        if device_owner == q_const.DEVICE_OWNER_ROUTER_SNAT:
            self._bind_centralized_snat_port_on_dvr_subnet(port, fixed_ips,
                                                           device_owner,
                                                           local_vlan_id)

    def _unbind_distributed_router_interface_port(self, port, local_vlan):

        ovsport = self.local_ports[port.vif_id]

        # removal of distributed router interface
        subnet_ids = ovsport.get_subnets()
        subnet_set = set(subnet_ids)
        # ensure we process for all the subnets laid on this removed port
        for sub_uuid in subnet_set:
            if sub_uuid not in self.local_dvr_map:
                continue

            ldm = self.local_dvr_map[sub_uuid]
            subnet_info = ldm.get_subnet_info()
            ip_subnet = subnet_info['cidr']

            # DVR is no more owner
            ldm.set_dvr_owned(False)

            # remove all vm rules for this dvr subnet
            # clear of compute_ports altogether
            compute_ports = ldm.get_compute_ofports()
            for vif_id in compute_ports:
                ovsport = self.local_ports[vif_id]
                self.int_br.delete_flows(table=constants.DVR_TO_SRC_MAC,
                                         dl_vlan=local_vlan,
                                         dl_dst=ovsport.get_mac())
            ldm.remove_all_compute_ofports()

            if ldm.get_csnat_ofport() != -1:
                # If there is a csnat port on this agent, preserve
                # the local_dvr_map state
                ofports = str(ldm.get_csnat_ofport())
                self.int_br.add_flow(table=constants.DVR_TO_SRC_MAC,
                                     priority=2,
                                     proto='ip',
                                     dl_vlan=local_vlan,
                                     nw_dst=ip_subnet,
                                     actions="strip_vlan,mod_dl_src:%s,"
                                     " output:%s" %
                                     (subnet_info['gateway_mac'], ofports))
            else:
                # removed port is a distributed router interface
                self.int_br.delete_flows(table=constants.DVR_TO_SRC_MAC,
                                         proto='ip', dl_vlan=local_vlan,
                                         nw_dst=ip_subnet)
                # remove subnet from local_dvr_map as no dvr (or) csnat
                # ports available on this agent anymore
                self.local_dvr_map.pop(sub_uuid, None)

            self.tun_br.delete_flows(table=constants.DVR_PROCESS,
                                     dl_vlan=local_vlan,
                                     proto='arp',
                                     nw_dst=subnet_info['gateway_ip'])
            ovsport.remove_subnet(sub_uuid)

        self.tun_br.delete_flows(table=constants.DVR_PROCESS,
                                 dl_vlan=local_vlan,
                                 dl_dst=port.vif_mac)

        self.tun_br.delete_flows(table=constants.DVR_PROCESS,
                                 dl_vlan=local_vlan,
                                 dl_src=port.vif_mac)
        # release port state
        self.local_ports.pop(port.vif_id, None)

    def _unbind_compute_port_on_dvr_subnet(self, port, local_vlan):

        ovsport = self.local_ports[port.vif_id]
        # This confirms that this compute port being removed belonged
        # to a dvr hosted subnet.
        # Accomodate this VM Port into the existing rule in
        # the integration bridge
        LOG.debug("DVR: Removing plumbing for compute port %s", port)
        subnet_ids = ovsport.get_subnets()
        # ensure we process for all the subnets laid on this port
        for sub_uuid in subnet_ids:
            if sub_uuid not in self.local_dvr_map:
                continue

            ldm = self.local_dvr_map[sub_uuid]
            subnet_info = ldm.get_subnet_info()
            ldm.remove_compute_ofport(port.vif_id)
            ofports = ','.join(map(str, ldm.get_compute_ofports().values()))
            ip_subnet = subnet_info['cidr']

            # first remove this vm port rule
            self.int_br.delete_flows(table=constants.DVR_TO_SRC_MAC,
                                     dl_vlan=local_vlan,
                                     dl_dst=ovsport.get_mac())
            if ldm.get_csnat_ofport() != -1:
                # If there is a csnat port on this agent, preserve
                # the local_dvr_map state
                ofports = str(ldm.get_csnat_ofport()) + ',' + ofports
                self.int_br.add_flow(table=constants.DVR_TO_SRC_MAC,
                                     priority=2,
                                     proto='ip',
                                     dl_vlan=local_vlan,
                                     nw_dst=ip_subnet,
                                     actions="strip_vlan,mod_dl_src:%s,"
                                     " output:%s" %
                                     (subnet_info['gateway_mac'], ofports))
            else:
                if ofports:
                    self.int_br.add_flow(table=constants.DVR_TO_SRC_MAC,
                                         priority=2,
                                         proto='ip',
                                         dl_vlan=local_vlan,
                                         nw_dst=ip_subnet,
                                         actions="strip_vlan,mod_dl_src:%s,"
                                         " output:%s" %
                                         (subnet_info['gateway_mac'],
                                          ofports))
                else:
                    # remove the flow altogether, as no ports (both csnat/
                    # compute) are available on this subnet in this
                    # agent
                    self.int_br.delete_flows(table=constants.DVR_TO_SRC_MAC,
                                             proto='ip',
                                             dl_vlan=local_vlan,
                                             nw_dst=ip_subnet)
        # release port state
        self.local_ports.pop(port.vif_id, None)

    def _unbind_centralized_snat_port_on_dvr_subnet(self, port, local_vlan):

        ovsport = self.local_ports[port.vif_id]
        # This comfirms that this compute port being removed belonged
        # to a dvr hosted subnet.
        # Accomodate this VM Port into the existing rule in
        # the integration bridge
        LOG.debug("DVR: Removing plumbing for csnat port %s", port)
        sub_uuid = list(ovsport.get_subnets())[0]
        # ensure we process for all the subnets laid on this port
        if sub_uuid not in self.local_dvr_map:
            return
        ldm = self.local_dvr_map[sub_uuid]
        subnet_info = ldm.get_subnet_info()
        ip_subnet = subnet_info['cidr']
        ldm.set_csnat_ofport(constants.OFPORT_INVALID)
        # then remove csnat port rule
        self.int_br.delete_flows(table=constants.DVR_TO_SRC_MAC,
                                 dl_vlan=local_vlan,
                                 dl_dst=ovsport.get_mac())

        ofports = ','.join(map(str, ldm.get_compute_ofports().values()))
        if ofports:
            self.int_br.add_flow(table=constants.DVR_TO_SRC_MAC,
                                 priority=2,
                                 proto='ip',
                                 dl_vlan=local_vlan,
                                 nw_dst=ip_subnet,
                                 actions="strip_vlan,mod_dl_src:%s,"
                                 " output:%s" %
                                 (subnet_info['gateway_mac'], ofports))
        else:
            self.int_br.delete_flows(table=constants.DVR_TO_SRC_MAC,
                                     proto='ip',
                                     dl_vlan=local_vlan,
                                     nw_dst=ip_subnet)
        if not ldm.is_dvr_owned():
            # if not owned by DVR (only used for csnat), remove this
            # subnet state altogether
            self.local_dvr_map.pop(sub_uuid, None)

        # release port state
        self.local_ports.pop(port.vif_id, None)

    def unbind_port_from_dvr(self, vif_port, local_vlan_id):
        if not self.enable_tunneling:
            return

        if not self.enable_distributed_routing:
            return

        # Handle port removed use-case
        if vif_port and vif_port.vif_id not in self.local_ports:
            LOG.debug("DVR: Non distributed port, ignoring %s", vif_port)
            return

        ovsport = self.local_ports[vif_port.vif_id]
        device_owner = ovsport.get_device_owner()

        if device_owner == q_const.DEVICE_OWNER_DVR_INTERFACE:
            self._unbind_distributed_router_interface_port(vif_port,
                                                           local_vlan_id)

        if device_owner and device_owner.startswith('compute:'):
            self._unbind_compute_port_on_dvr_subnet(vif_port,
                                                    local_vlan_id)

        if device_owner == q_const.DEVICE_OWNER_ROUTER_SNAT:
            self._unbind_centralized_snat_port_on_dvr_subnet(vif_port,
                                                             local_vlan_id)
