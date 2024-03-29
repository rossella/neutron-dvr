# Copyright (c) 2013 OpenStack Foundation.
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

from oslo.config import cfg
import sqlalchemy as sa
from sqlalchemy import func
from sqlalchemy import orm
from sqlalchemy.orm import exc
from sqlalchemy.orm import joinedload

from neutron.common import constants
from neutron.db import agents_db
from neutron.db import agentschedulers_db
from neutron.db import model_base
from neutron.db import models_v2
from neutron.extensions import l3agentscheduler
from neutron import manager

L3_AGENTS_SCHEDULER_OPTS = [
    cfg.StrOpt('router_scheduler_driver',
               default='neutron.scheduler.l3_agent_scheduler.ChanceScheduler',
               help=_('Driver to use for scheduling '
                      'router to a default L3 agent')),
    cfg.BoolOpt('router_auto_schedule', default=True,
                help=_('Allow auto scheduling of routers to L3 agent.')),
]

cfg.CONF.register_opts(L3_AGENTS_SCHEDULER_OPTS)


class RouterL3AgentBinding(model_base.BASEV2, models_v2.HasId):
    """Represents binding between neutron routers and L3 agents."""

    router_id = sa.Column(sa.String(36),
                          sa.ForeignKey("routers.id", ondelete='CASCADE'))
    l3_agent = orm.relation(agents_db.Agent)
    l3_agent_id = sa.Column(sa.String(36),
                            sa.ForeignKey("agents.id",
                                          ondelete='CASCADE'))


class L3AgentSchedulerDbMixin(l3agentscheduler.L3AgentSchedulerPluginBase,
                              agentschedulers_db.AgentSchedulerDbMixin):
    """Mixin class to add l3 agent scheduler extension to plugins
    using the l3 agent for routing.
    """

    router_scheduler = None

    def add_router_to_l3_agent(self, context, agent_id, router_id):
        """Add a l3 agent to host a router."""
        router = self.get_router(context, router_id)
        router_type = (
            'distributed' if router.get('distributed') else 'centralized')
        with context.session.begin(subtransactions=True):
            agent_db = self._get_agent(context, agent_id)
            agent_conf = self.get_configuration_dict(agent_db)
            agent_mode = agent_conf.get('agent_mode', 'legacy')
            if (router_type == 'centralized' and agent_mode.startswith('dvr')
                or router_type == 'distributed' and agent_mode == 'legacy'):
                raise l3agentscheduler.RouterL3AgentMismatch(
                    router_type=router_type, router_id=router_id,
                    agent_mode=agent_mode, agent_id=agent_id)
            if (agent_db['agent_type'] != constants.AGENT_TYPE_L3 or
                not agent_db['admin_state_up'] or
                not self.get_l3_agent_candidates(context,
                                                 router,
                                                 [agent_db])):
                raise l3agentscheduler.InvalidL3Agent(id=agent_id)
            query = context.session.query(RouterL3AgentBinding)
            if router_type == 'distributed':
                binding = query.filter_by(router_id=router_id,
                                          l3_agent_id=agent_id).first()
                if binding:
                    raise l3agentscheduler.RouterHostedByL3Agent(
                        router_id=router_id,
                        agent_id=binding.l3_agent_id)
            else:
                try:
                    binding = query.filter_by(router_id=router_id).one()

                    raise l3agentscheduler.RouterHostedByL3Agent(
                        router_id=router_id,
                        agent_id=binding.l3_agent_id)
                except exc.NoResultFound:
                    pass

            result = self.auto_schedule_routers(context,
                                                agent_db.host,
                                                [router_id])
            if not result:
                raise l3agentscheduler.RouterSchedulingFailed(
                    router_id=router_id, agent_id=agent_id)

        l3_notifier = self.agent_notifiers.get(constants.AGENT_TYPE_L3)
        if l3_notifier:
            l3_notifier.router_added_to_agent(
                context, [router_id], agent_db.host)

    def remove_router_from_l3_agent(self, context, agent_id, router_id):
        """Remove the router from l3 agent.

        After removal, the router will be non-hosted until there is update
        which leads to re-schedule or be added to another agent manually.
        """
        agent = self._get_agent(context, agent_id)
        floating_ips = self._get_sync_floating_ips(context, [router_id])
        if floating_ips:
            raise l3agentscheduler.RemoveFloatingIPforRouter(
                router_id=router_id, agent_id=agent_id)
        self._unbind_router(context, router_id, agent_id)
        l3_notifier = self.agent_notifiers.get(constants.AGENT_TYPE_L3)
        if l3_notifier:
            l3_notifier.router_removed_from_agent(
                context, router_id, agent.host)

    def _unbind_router(self, context, router_id, agent_id):
        with context.session.begin(subtransactions=True):
            query = context.session.query(RouterL3AgentBinding)
            query = query.filter(
                RouterL3AgentBinding.router_id == router_id,
                RouterL3AgentBinding.l3_agent_id == agent_id)
            try:
                binding = query.one()
            except exc.NoResultFound:
                raise l3agentscheduler.RouterNotHostedByL3Agent(
                    router_id=router_id, agent_id=agent_id)
            context.session.delete(binding)

    def reschedule_router(self, context, router_id, candidates=None):
        """Reschedule router to a new l3 agent

        Remove the router from the agent(s) currently hosting it and
        schedule it again
        """
        cur_agents = self.list_l3_agents_hosting_router(
            context, router_id)['agents']
        with context.session.begin(subtransactions=True):
            for agent in cur_agents:
                self._unbind_router(context, router_id, agent['id'])

            new_agent = self.schedule_router(context, router_id,
                                             candidates=candidates)
            if not new_agent:
                raise l3agentscheduler.RouterReschedulingFailed(
                    router_id=router_id)

        l3_notifier = self.agent_notifiers.get(constants.AGENT_TYPE_L3)
        if l3_notifier:
            for agent in cur_agents:
                l3_notifier.router_removed_from_agent(
                    context, router_id, agent['host'])
            l3_notifier.router_added_to_agent(
                context, [router_id], new_agent.host)

    def list_routers_on_l3_agent(self, context, agent_id):
        query = context.session.query(RouterL3AgentBinding.router_id)
        query = query.filter(RouterL3AgentBinding.l3_agent_id == agent_id)

        router_ids = [item[0] for item in query]
        if router_ids:
            return {'routers':
                    self.get_routers(context, filters={'id': router_ids})}
        else:
            return {'routers': []}

    def list_active_sync_routers_on_active_l3_agent(
            self, context, host, router_ids):
        agent = self._get_agent_by_type_and_host(
            context, constants.AGENT_TYPE_L3, host)
        if not agent.admin_state_up:
            return []
        query = context.session.query(RouterL3AgentBinding.router_id)
        query = query.filter(
            RouterL3AgentBinding.l3_agent_id == agent.id)

        if not router_ids:
            pass
        else:
            query = query.filter(
                RouterL3AgentBinding.router_id.in_(router_ids))
        router_ids = [item[0] for item in query]
        if router_ids:
            return self.get_sync_data(context, router_ids=router_ids,
                                      active=True)
        else:
            return []

    def get_l3_agents_hosting_routers(self, context, router_ids,
                                      admin_state_up=None,
                                      active=None):
        if not router_ids:
            return []
        query = context.session.query(RouterL3AgentBinding)
        if len(router_ids) > 1:
            query = query.options(joinedload('l3_agent')).filter(
                RouterL3AgentBinding.router_id.in_(router_ids))
        else:
            query = query.options(joinedload('l3_agent')).filter(
                RouterL3AgentBinding.router_id == router_ids[0])
        if admin_state_up is not None:
            query = (query.filter(agents_db.Agent.admin_state_up ==
                                  admin_state_up))
        l3_agents = [binding.l3_agent for binding in query]
        if active is not None:
            l3_agents = [l3_agent for l3_agent in
                         l3_agents if not
                         agents_db.AgentDbMixin.is_agent_down(
                             l3_agent['heartbeat_timestamp'])]
        return l3_agents

    def _get_l3_bindings_hosting_routers(self, context, router_ids):
        if not router_ids:
            return []
        query = context.session.query(RouterL3AgentBinding)
        if len(router_ids) > 1:
            query = query.options(joinedload('l3_agent')).filter(
                RouterL3AgentBinding.router_id.in_(router_ids))
        else:
            query = query.options(joinedload('l3_agent')).filter(
                RouterL3AgentBinding.router_id == router_ids[0])
        return query.all()

    def list_l3_agents_hosting_router(self, context, router_id):
        with context.session.begin(subtransactions=True):
            bindings = self._get_l3_bindings_hosting_routers(
                context, [router_id])
            results = []
            for binding in bindings:
                l3_agent_dict = self._make_agent_dict(binding.l3_agent)
                results.append(l3_agent_dict)
            if results:
                return {'agents': results}
            else:
                return {'agents': []}

    def get_l3_agents(self, context, active=None, filters=None):
        query = context.session.query(agents_db.Agent)
        query = query.filter(
            agents_db.Agent.agent_type == constants.AGENT_TYPE_L3)
        if active is not None:
            query = (query.filter(agents_db.Agent.admin_state_up == active))
        if filters:
            for key, value in filters.iteritems():
                column = getattr(agents_db.Agent, key, None)
                if column:
                    query = query.filter(column.in_(value))

        return [l3_agent
                for l3_agent in query
                if agentschedulers_db.AgentSchedulerDbMixin.is_eligible_agent(
                    active, l3_agent)]

    def list_interfaces_onrouter(self, context, router_id):
        if router_id:
            return (context.session.query(models_v2.Port).
                    filter_by(device_id=router_id).all())

    def check_vmexists_on_l3agent(self, context, l3_agent, router_id,
                                  subnet_id):
        core_plugin = manager.NeutronManager.get_plugin()
        if not subnet_id:
            return True
        filter = {'fixed_ips': {'subnet_id': [subnet_id]}}
        ports = core_plugin.get_ports(context, filters=filter)
        for port in ports:
            if "compute:" in port['device_owner']:
                if l3_agent['host'] == port['binding:host_id']:
                    return True
        return False

    def get_snat_candidates(self, sync_router, l3_agents):
        """Get the valid snat enabled l3 agents for the distributed router."""
        candidates = []
        is_router_distributed = sync_router.get('distributed', False)
        if not is_router_distributed:
            return []
        for l3_agent in l3_agents:
            if not l3_agent.admin_state_up:
                continue

            agent_conf = self.get_configuration_dict(l3_agent)
            router_id = agent_conf.get('router_id', None)
            use_namespaces = agent_conf.get('use_namespaces', True)
            handle_internal_only_routers = agent_conf.get(
                'handle_internal_only_routers', True)
            gateway_external_network_id = agent_conf.get(
                'gateway_external_network_id', None)
            agent_mode = agent_conf.get('agent_mode', 'legacy')

            if agent_mode != 'dvr_snat':
                continue
            if not use_namespaces and router_id != sync_router['id']:
                continue
            ex_net_id = (sync_router['external_gateway_info'] or {}).get(
                'network_id')
            if ((not ex_net_id and not handle_internal_only_routers) or
                (ex_net_id and gateway_external_network_id and
                 ex_net_id != gateway_external_network_id)):
                continue

            if is_router_distributed and agent_mode.startswith('dvr'):
                candidates.append(l3_agent)
        return candidates

    def get_l3_agent_candidates(self, context, sync_router, l3_agents,
                                subnet_id=None):
        """Get the valid l3 agents for the router from a list of l3_agents."""
        candidates = []
        for l3_agent in l3_agents:
            if not l3_agent.admin_state_up:
                continue
            agent_conf = self.get_configuration_dict(l3_agent)
            router_id = agent_conf.get('router_id', None)
            use_namespaces = agent_conf.get('use_namespaces', True)
            handle_internal_only_routers = agent_conf.get(
                'handle_internal_only_routers', True)
            gateway_external_network_id = agent_conf.get(
                'gateway_external_network_id', None)
            agent_mode = agent_conf.get('agent_mode', 'legacy')
            if not use_namespaces and router_id != sync_router['id']:
                continue
            ex_net_id = (sync_router['external_gateway_info'] or {}).get(
                'network_id')
            if ((not ex_net_id and not handle_internal_only_routers) or
                (ex_net_id and gateway_external_network_id and
                 ex_net_id != gateway_external_network_id)):
                continue
            is_router_distributed = sync_router.get('distributed', False)
            if not is_router_distributed and agent_mode == 'legacy':
                candidates.append(l3_agent)
            elif (agent_mode.startswith('dvr') and
                self.check_vmexists_on_l3agent(
                    context, l3_agent, sync_router['id'], subnet_id)):
                candidates.append(l3_agent)
        return candidates

    def auto_schedule_routers(self, context, host, router_ids):
        if self.router_scheduler:
            return self.router_scheduler.auto_schedule_routers(
                self, context, host, router_ids)

    def schedule_router(self, context, router, candidates=None, hints=None):
        if self.router_scheduler:
            return self.router_scheduler.schedule(
                self, context, router, candidates=candidates, hints=hints)

    def schedule_routers(self, context, routers, hints=None):
        """Schedule the routers to l3 agents."""
        for router in routers:
            self.schedule_router(context, router, candidates=None, hints=hints)

    def get_l3_agent_with_min_routers(self, context, agent_ids):
        """Return l3 agent with the least number of routers."""
        query = context.session.query(
            agents_db.Agent,
            func.count(
                RouterL3AgentBinding.router_id
            ).label('count')).outerjoin(RouterL3AgentBinding).group_by(
                RouterL3AgentBinding.l3_agent_id).order_by('count')
        res = query.filter(agents_db.Agent.id.in_(agent_ids)).first()
        return res[0]
