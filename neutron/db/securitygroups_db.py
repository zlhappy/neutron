# Copyright 2012 VMware, Inc.  All rights reserved.
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

import netaddr
from neutron_lib.api import validators
from neutron_lib import constants
from neutron_lib.utils import helpers
from oslo_utils import uuidutils
from sqlalchemy.orm import exc
from sqlalchemy.orm import scoped_session

from neutron._i18n import _
from neutron.api.v2 import attributes
from neutron.callbacks import events
from neutron.callbacks import exceptions
from neutron.callbacks import registry
from neutron.callbacks import resources
from neutron.common import constants as n_const
from neutron.common import utils
from neutron.db import _utils as db_utils
from neutron.db import api as db_api
from neutron.db import db_base_plugin_v2
from neutron.db.models import securitygroup as sg_models
from neutron.extensions import securitygroup as ext_sg


@registry.has_registry_receivers
class SecurityGroupDbMixin(ext_sg.SecurityGroupPluginBase):
    """Mixin class to add security group to db_base_plugin_v2."""

    __native_bulk_support = True

    def create_security_group_bulk(self, context, security_groups):
        return self._create_bulk('security_group', context,
                                 security_groups)

    def _registry_notify(self, res, event, id=None, exc_cls=None, **kwargs):
        # NOTE(armax): a callback exception here will prevent the request
        # from being processed. This is a hook point for backend's validation;
        # we raise to propagate the reason for the failure.
        try:
            registry.notify(res, event, self, **kwargs)
        except exceptions.CallbackFailure as e:
            if exc_cls:
                reason = (_('cannot perform %(event)s due to %(reason)s') %
                          {'event': event, 'reason': e})
                raise exc_cls(reason=reason, id=id)

    @db_api.retry_if_session_inactive()
    def create_security_group(self, context, security_group, default_sg=False):
        """Create security group.

        If default_sg is true that means we are a default security group for
        a given tenant if it does not exist.
        """
        s = security_group['security_group']
        kwargs = {
            'context': context,
            'security_group': s,
            'is_default': default_sg,
        }

        self._registry_notify(resources.SECURITY_GROUP, events.BEFORE_CREATE,
                              exc_cls=ext_sg.SecurityGroupConflict, **kwargs)

        tenant_id = s['tenant_id']

        if not default_sg:
            self._ensure_default_security_group(context, tenant_id)
        else:
            existing_def_sg_id = self._get_default_sg_id(context, tenant_id)
            if existing_def_sg_id is not None:
                # default already exists, return it
                return self.get_security_group(context, existing_def_sg_id)

        with db_api.autonested_transaction(context.session):
            security_group_db = sg_models.SecurityGroup(id=s.get('id') or (
                                              uuidutils.generate_uuid()),
                                              description=s['description'],
                                              tenant_id=tenant_id,
                                              name=s['name'])
            context.session.add(security_group_db)
            if default_sg:
                context.session.add(sg_models.DefaultSecurityGroup(
                    security_group=security_group_db,
                    tenant_id=security_group_db['tenant_id']))
            for ethertype in ext_sg.sg_supported_ethertypes:
                if default_sg:
                    # Allow intercommunication
                    ingress_rule = sg_models.SecurityGroupRule(
                        id=uuidutils.generate_uuid(), tenant_id=tenant_id,
                        security_group=security_group_db,
                        direction='ingress',
                        ethertype=ethertype,
                        source_group=security_group_db)
                    context.session.add(ingress_rule)

                egress_rule = sg_models.SecurityGroupRule(
                    id=uuidutils.generate_uuid(), tenant_id=tenant_id,
                    security_group=security_group_db,
                    direction='egress',
                    ethertype=ethertype)
                context.session.add(egress_rule)

            self._registry_notify(resources.SECURITY_GROUP,
                                  events.PRECOMMIT_CREATE,
                                  exc_cls=ext_sg.SecurityGroupConflict,
                                  **kwargs)

        secgroup_dict = self._make_security_group_dict(security_group_db)

        kwargs['security_group'] = secgroup_dict
        registry.notify(resources.SECURITY_GROUP, events.AFTER_CREATE, self,
                        **kwargs)
        return secgroup_dict

    @db_api.retry_if_session_inactive()
    def get_security_groups(self, context, filters=None, fields=None,
                            sorts=None, limit=None,
                            marker=None, page_reverse=False, default_sg=False):

        # If default_sg is True do not call _ensure_default_security_group()
        # so this can be done recursively. Context.tenant_id is checked
        # because all the unit tests do not explicitly set the context on
        # GETS. TODO(arosen)  context handling can probably be improved here.
        if not default_sg and context.tenant_id:
            tenant_id = filters.get('tenant_id')
            if tenant_id:
                tenant_id = tenant_id[0]
            else:
                tenant_id = context.tenant_id
            self._ensure_default_security_group(context, tenant_id)
        marker_obj = self._get_marker_obj(context, 'security_group', limit,
                                          marker)
        return self._get_collection(context,
                                    sg_models.SecurityGroup,
                                    self._make_security_group_dict,
                                    filters=filters, fields=fields,
                                    sorts=sorts,
                                    limit=limit, marker_obj=marker_obj,
                                    page_reverse=page_reverse)

    @db_api.retry_if_session_inactive()
    def get_security_groups_count(self, context, filters=None):
        return self._get_collection_count(context, sg_models.SecurityGroup,
                                          filters=filters)

    @db_api.retry_if_session_inactive()
    def get_security_group(self, context, id, fields=None, tenant_id=None):
        """Tenant id is given to handle the case when creating a security
        group rule on behalf of another use.
        """

        if tenant_id:
            tmp_context_tenant_id = context.tenant_id
            context.tenant_id = tenant_id

        try:
            with context.session.begin(subtransactions=True):
                ret = self._make_security_group_dict(self._get_security_group(
                                                     context, id), fields)
                ret['security_group_rules'] = self.get_security_group_rules(
                    context, {'security_group_id': [id]})
        finally:
            if tenant_id:
                context.tenant_id = tmp_context_tenant_id
        return ret

    def _get_security_group(self, context, id):
        try:
            query = self._model_query(context, sg_models.SecurityGroup)
            sg = query.filter(sg_models.SecurityGroup.id == id).one()

        except exc.NoResultFound:
            raise ext_sg.SecurityGroupNotFound(id=id)
        return sg

    @db_api.retry_if_session_inactive()
    def delete_security_group(self, context, id):
        filters = {'security_group_id': [id]}
        ports = self._get_port_security_group_bindings(context, filters)
        if ports:
            raise ext_sg.SecurityGroupInUse(id=id)
        # confirm security group exists
        sg = self._get_security_group(context, id)

        if sg['name'] == 'default' and not context.is_admin:
            raise ext_sg.SecurityGroupCannotRemoveDefault()
        kwargs = {
            'context': context,
            'security_group_id': id,
            'security_group': sg,
        }
        self._registry_notify(resources.SECURITY_GROUP, events.BEFORE_DELETE,
                              exc_cls=ext_sg.SecurityGroupInUse, id=id,
                              **kwargs)

        with context.session.begin(subtransactions=True):
            # pass security_group_rule_ids to ensure
            # consistency with deleted rules
            kwargs['security_group_rule_ids'] = [r['id'] for r in sg.rules]
            self._registry_notify(resources.SECURITY_GROUP,
                                  events.PRECOMMIT_DELETE,
                                  exc_cls=ext_sg.SecurityGroupInUse, id=id,
                                  **kwargs)
            context.session.delete(sg)

        kwargs.pop('security_group')
        registry.notify(resources.SECURITY_GROUP, events.AFTER_DELETE, self,
                        **kwargs)

    @db_api.retry_if_session_inactive()
    def update_security_group(self, context, id, security_group):
        s = security_group['security_group']

        kwargs = {
            'context': context,
            'security_group_id': id,
            'security_group': s,
        }
        self._registry_notify(resources.SECURITY_GROUP, events.BEFORE_UPDATE,
                              exc_cls=ext_sg.SecurityGroupConflict, **kwargs)

        with context.session.begin(subtransactions=True):
            sg = self._get_security_group(context, id)
            if sg['name'] == 'default' and 'name' in s:
                raise ext_sg.SecurityGroupCannotUpdateDefault()
            self._registry_notify(
                    resources.SECURITY_GROUP,
                    events.PRECOMMIT_UPDATE,
                    exc_cls=ext_sg.SecurityGroupConflict, **kwargs)
            sg.update(s)
        sg_dict = self._make_security_group_dict(sg)

        kwargs['security_group'] = sg_dict
        registry.notify(resources.SECURITY_GROUP, events.AFTER_UPDATE, self,
                        **kwargs)
        return sg_dict

    def _make_security_group_dict(self, security_group, fields=None):
        res = {'id': security_group['id'],
               'name': security_group['name'],
               'tenant_id': security_group['tenant_id'],
               'description': security_group['description']}
        res['security_group_rules'] = [self._make_security_group_rule_dict(r)
                                       for r in security_group.rules]
        self._apply_dict_extend_functions(ext_sg.SECURITYGROUPS, res,
                                          security_group)
        return db_utils.resource_fields(res, fields)

    @staticmethod
    def _make_security_group_binding_dict(security_group, fields=None):
        res = {'port_id': security_group['port_id'],
               'security_group_id': security_group['security_group_id']}
        return db_utils.resource_fields(res, fields)

    @db_api.retry_if_session_inactive()
    def _create_port_security_group_binding(self, context, port_id,
                                            security_group_id):
        with context.session.begin(subtransactions=True):
            db = sg_models.SecurityGroupPortBinding(port_id=port_id,
                                          security_group_id=security_group_id)
            context.session.add(db)

    def _get_port_security_group_bindings(self, context,
                                          filters=None, fields=None):
        return self._get_collection(context,
                                    sg_models.SecurityGroupPortBinding,
                                    self._make_security_group_binding_dict,
                                    filters=filters, fields=fields)

    @db_api.retry_if_session_inactive()
    def _delete_port_security_group_bindings(self, context, port_id):
        query = self._model_query(context, sg_models.SecurityGroupPortBinding)
        bindings = query.filter(
            sg_models.SecurityGroupPortBinding.port_id == port_id)
        with context.session.begin(subtransactions=True):
            for binding in bindings:
                context.session.delete(binding)

    @db_api.retry_if_session_inactive()
    def create_security_group_rule_bulk(self, context, security_group_rules):
        return self._create_bulk('security_group_rule', context,
                                 security_group_rules)

    @db_api.retry_if_session_inactive()
    def create_security_group_rule_bulk_native(self, context,
                                               security_group_rules):
        rules = security_group_rules['security_group_rules']
        scoped_session(context.session)
        security_group_id = self._validate_security_group_rules(
            context, security_group_rules)
        with context.session.begin(subtransactions=True):
            if not self.get_security_group(context, security_group_id):
                raise ext_sg.SecurityGroupNotFound(id=security_group_id)

            self._check_for_duplicate_rules(context, rules)
            ret = []
            for rule_dict in rules:
                res_rule_dict = self._create_security_group_rule(
                    context, rule_dict, validate=False)
                ret.append(res_rule_dict)
        for rdict in ret:
            registry.notify(
                resources.SECURITY_GROUP_RULE, events.AFTER_CREATE, self,
                context=context, security_group_rule=rdict)
        return ret

    @db_api.retry_if_session_inactive()
    def create_security_group_rule(self, context, security_group_rule):
        res = self._create_security_group_rule(context, security_group_rule)
        registry.notify(
            resources.SECURITY_GROUP_RULE, events.AFTER_CREATE, self,
            context=context, security_group_rule=res)
        return res

    def _create_security_group_rule(self, context, security_group_rule,
                                    validate=True):
        if validate:
            self._validate_security_group_rule(context, security_group_rule)
        rule_dict = security_group_rule['security_group_rule']
        kwargs = {
            'context': context,
            'security_group_rule': rule_dict
        }
        self._registry_notify(resources.SECURITY_GROUP_RULE,
                              events.BEFORE_CREATE,
                              exc_cls=ext_sg.SecurityGroupConflict, **kwargs)

        with context.session.begin(subtransactions=True):
            if validate:
                self._check_for_duplicate_rules_in_db(context,
                                                      security_group_rule)
            db = sg_models.SecurityGroupRule(
                id=(rule_dict.get('id') or uuidutils.generate_uuid()),
                tenant_id=rule_dict['tenant_id'],
                security_group_id=rule_dict['security_group_id'],
                direction=rule_dict['direction'],
                remote_group_id=rule_dict.get('remote_group_id'),
                ethertype=rule_dict['ethertype'],
                protocol=rule_dict['protocol'],
                port_range_min=rule_dict['port_range_min'],
                port_range_max=rule_dict['port_range_max'],
                remote_ip_prefix=rule_dict.get('remote_ip_prefix'),
                description=rule_dict.get('description')
            )
            context.session.add(db)
            self._registry_notify(resources.SECURITY_GROUP_RULE,
                              events.PRECOMMIT_CREATE,
                              exc_cls=ext_sg.SecurityGroupConflict, **kwargs)
        return self._make_security_group_rule_dict(db)

    def _get_ip_proto_number(self, protocol):
        if protocol is None:
            return
        # According to bug 1381379, protocol is always set to string to avoid
        # problems with comparing int and string in PostgreSQL. Here this
        # string is converted to int to give an opportunity to use it as
        # before.
        if protocol in n_const.IP_PROTOCOL_NAME_ALIASES:
            protocol = n_const.IP_PROTOCOL_NAME_ALIASES[protocol]
        return int(constants.IP_PROTOCOL_MAP.get(protocol, protocol))

    def _get_ip_proto_name_and_num(self, protocol):
        if protocol is None:
            return
        protocol = str(protocol)
        if protocol in constants.IP_PROTOCOL_MAP:
            return [protocol, str(constants.IP_PROTOCOL_MAP.get(protocol))]
        elif protocol in n_const.IP_PROTOCOL_NUM_TO_NAME_MAP:
            return [n_const.IP_PROTOCOL_NUM_TO_NAME_MAP.get(protocol),
                    protocol]
        return [protocol, protocol]

    def _validate_port_range(self, rule):
        """Check that port_range is valid."""
        if (rule['port_range_min'] is None and
            rule['port_range_max'] is None):
            return
        if not rule['protocol']:
            raise ext_sg.SecurityGroupProtocolRequiredWithPorts()
        ip_proto = self._get_ip_proto_number(rule['protocol'])
        if ip_proto in [constants.PROTO_NUM_TCP, constants.PROTO_NUM_UDP]:
            if rule['port_range_min'] == 0 or rule['port_range_max'] == 0:
                raise ext_sg.SecurityGroupInvalidPortValue(port=0)
            elif (rule['port_range_min'] is not None and
                rule['port_range_max'] is not None and
                rule['port_range_min'] <= rule['port_range_max']):
                pass
            else:
                raise ext_sg.SecurityGroupInvalidPortRange()
        elif ip_proto in [constants.PROTO_NUM_ICMP,
                          constants.PROTO_NUM_IPV6_ICMP]:
            for attr, field in [('port_range_min', 'type'),
                                ('port_range_max', 'code')]:
                if rule[attr] is not None and not (0 <= rule[attr] <= 255):
                    raise ext_sg.SecurityGroupInvalidIcmpValue(
                        field=field, attr=attr, value=rule[attr])
            if (rule['port_range_min'] is None and
                    rule['port_range_max'] is not None):
                raise ext_sg.SecurityGroupMissingIcmpType(
                    value=rule['port_range_max'])

    def _validate_ethertype_and_protocol(self, rule):
        """Check if given ethertype and  protocol are valid or not"""
        if rule['protocol'] in [constants.PROTO_NAME_IPV6_ENCAP,
                                constants.PROTO_NAME_IPV6_FRAG,
                                constants.PROTO_NAME_IPV6_ICMP,
                                constants.PROTO_NAME_IPV6_ICMP_LEGACY,
                                constants.PROTO_NAME_IPV6_NONXT,
                                constants.PROTO_NAME_IPV6_OPTS,
                                constants.PROTO_NAME_IPV6_ROUTE]:
            if rule['ethertype'] == constants.IPv4:
                raise ext_sg.SecurityGroupEthertypeConflictWithProtocol(
                        ethertype=rule['ethertype'], protocol=rule['protocol'])

    def _validate_single_tenant_and_group(self, security_group_rules):
        """Check that all rules belong to the same security group and tenant
        """
        sg_groups = set()
        tenants = set()
        for rule_dict in security_group_rules['security_group_rules']:
            rule = rule_dict['security_group_rule']
            sg_groups.add(rule['security_group_id'])
            if len(sg_groups) > 1:
                raise ext_sg.SecurityGroupNotSingleGroupRules()

            tenants.add(rule['tenant_id'])
            if len(tenants) > 1:
                raise ext_sg.SecurityGroupRulesNotSingleTenant()
        return sg_groups.pop()

    def _validate_security_group_rule(self, context, security_group_rule):
        rule = security_group_rule['security_group_rule']
        self._validate_port_range(rule)
        self._validate_ip_prefix(rule)
        self._validate_ethertype_and_protocol(rule)

        if rule['remote_ip_prefix'] and rule['remote_group_id']:
            raise ext_sg.SecurityGroupRemoteGroupAndRemoteIpPrefix()

        remote_group_id = rule['remote_group_id']
        # Check that remote_group_id exists for tenant
        if remote_group_id:
            self.get_security_group(context, remote_group_id,
                                    tenant_id=rule['tenant_id'])

        security_group_id = rule['security_group_id']

        # Confirm that the tenant has permission
        # to add rules to this security group.
        self.get_security_group(context, security_group_id,
                                tenant_id=rule['tenant_id'])
        return security_group_id

    def _validate_security_group_rules(self, context, security_group_rules):
        sg_id = self._validate_single_tenant_and_group(security_group_rules)
        for rule in security_group_rules['security_group_rules']:
            self._validate_security_group_rule(context, rule)
        return sg_id

    def _make_security_group_rule_dict(self, security_group_rule, fields=None):
        res = {'id': security_group_rule['id'],
               'tenant_id': security_group_rule['tenant_id'],
               'security_group_id': security_group_rule['security_group_id'],
               'ethertype': security_group_rule['ethertype'],
               'direction': security_group_rule['direction'],
               'protocol': security_group_rule['protocol'],
               'port_range_min': security_group_rule['port_range_min'],
               'port_range_max': security_group_rule['port_range_max'],
               'remote_ip_prefix': security_group_rule['remote_ip_prefix'],
               'remote_group_id': security_group_rule['remote_group_id']}

        self._apply_dict_extend_functions(ext_sg.SECURITYGROUPRULES, res,
                                          security_group_rule)
        return db_utils.resource_fields(res, fields)

    def _make_security_group_rule_filter_dict(self, security_group_rule):
        sgr = security_group_rule['security_group_rule']
        res = {'tenant_id': [sgr['tenant_id']],
               'security_group_id': [sgr['security_group_id']],
               'direction': [sgr['direction']]}

        include_if_present = ['protocol', 'port_range_max', 'port_range_min',
                              'ethertype', 'remote_group_id']
        for key in include_if_present:
            value = sgr.get(key)
            if value:
                res[key] = [value]
        # protocol field will get corresponding name and number
        value = sgr.get('protocol')
        if value:
            res['protocol'] = self._get_ip_proto_name_and_num(value)
        return res

    def _rules_equal(self, rule1, rule2):
        """Determines if two rules are equal ignoring id field."""
        rule1_copy = rule1.copy()
        rule2_copy = rule2.copy()
        rule1_copy.pop('id', None)
        rule2_copy.pop('id', None)
        return rule1_copy == rule2_copy

    def _check_for_duplicate_rules(self, context, security_group_rules):
        for i in security_group_rules:
            found_self = False
            for j in security_group_rules:
                if self._rules_equal(i['security_group_rule'],
                                     j['security_group_rule']):
                    if found_self:
                        raise ext_sg.DuplicateSecurityGroupRuleInPost(rule=i)
                    found_self = True

            self._check_for_duplicate_rules_in_db(context, i)

    def _check_for_duplicate_rules_in_db(self, context, security_group_rule):
        # Check in database if rule exists
        filters = self._make_security_group_rule_filter_dict(
            security_group_rule)
        rule_dict = security_group_rule['security_group_rule'].copy()
        rule_dict.pop('description', None)
        keys = rule_dict.keys()
        fields = list(keys) + ['id']
        if 'remote_ip_prefix' not in fields:
            fields += ['remote_ip_prefix']
        db_rules = self.get_security_group_rules(context, filters,
                                                 fields=fields)
        # Note(arosen): the call to get_security_group_rules wildcards
        # values in the filter that have a value of [None]. For
        # example, filters = {'remote_group_id': [None]} will return
        # all security group rules regardless of their value of
        # remote_group_id. Therefore it is not possible to do this
        # query unless the behavior of _get_collection()
        # is changed which cannot be because other methods are already
        # relying on this behavior. Therefore, we do the filtering
        # below to check for these corner cases.
        rule_dict.pop('id', None)
        sg_protocol = rule_dict.pop('protocol', None)
        remote_ip_prefix = rule_dict.pop('remote_ip_prefix', None)
        for db_rule in db_rules:
            rule_id = db_rule.pop('id', None)
            # remove protocol and match separately for number and type
            db_protocol = db_rule.pop('protocol', None)
            is_protocol_matching = (
                self._get_ip_proto_name_and_num(db_protocol) ==
                self._get_ip_proto_name_and_num(sg_protocol))
            db_remote_ip_prefix = db_rule.pop('remote_ip_prefix', None)
            duplicate_ip_prefix = self._validate_duplicate_ip_prefix(
                                    remote_ip_prefix, db_remote_ip_prefix)
            if (is_protocol_matching and duplicate_ip_prefix and
                    rule_dict == db_rule):
                raise ext_sg.SecurityGroupRuleExists(rule_id=rule_id)

    def _validate_duplicate_ip_prefix(self, ip_prefix, other_ip_prefix):
        all_address = ['0.0.0.0/0', '::/0', None]
        if ip_prefix == other_ip_prefix:
            return True
        elif ip_prefix in all_address and other_ip_prefix in all_address:
            return True
        return False

    def _validate_ip_prefix(self, rule):
        """Check that a valid cidr was specified as remote_ip_prefix

        No need to check that it is in fact an IP address as this is already
        validated by attribute validators.
        Check that rule ethertype is consistent with remote_ip_prefix ip type.
        Add mask to ip_prefix if absent (192.168.1.10 -> 192.168.1.10/32).
        """
        input_prefix = rule['remote_ip_prefix']
        if input_prefix:
            addr = netaddr.IPNetwork(input_prefix)
            # set input_prefix to always include the netmask:
            rule['remote_ip_prefix'] = str(addr)
            # check consistency of ethertype with addr version
            if rule['ethertype'] != "IPv%d" % (addr.version):
                raise ext_sg.SecurityGroupRuleParameterConflict(
                    ethertype=rule['ethertype'], cidr=input_prefix)

    @db_api.retry_if_session_inactive()
    def get_security_group_rules(self, context, filters=None, fields=None,
                                 sorts=None, limit=None, marker=None,
                                 page_reverse=False):
        marker_obj = self._get_marker_obj(context, 'security_group_rule',
                                          limit, marker)
        return self._get_collection(context,
                                    sg_models.SecurityGroupRule,
                                    self._make_security_group_rule_dict,
                                    filters=filters, fields=fields,
                                    sorts=sorts,
                                    limit=limit, marker_obj=marker_obj,
                                    page_reverse=page_reverse)

    @db_api.retry_if_session_inactive()
    def get_security_group_rules_count(self, context, filters=None):
        return self._get_collection_count(context, sg_models.SecurityGroupRule,
                                          filters=filters)

    @db_api.retry_if_session_inactive()
    def get_security_group_rule(self, context, id, fields=None):
        security_group_rule = self._get_security_group_rule(context, id)
        return self._make_security_group_rule_dict(security_group_rule, fields)

    def _get_security_group_rule(self, context, id):
        try:
            query = self._model_query(context, sg_models.SecurityGroupRule)
            sgr = query.filter(sg_models.SecurityGroupRule.id == id).one()
        except exc.NoResultFound:
            raise ext_sg.SecurityGroupRuleNotFound(id=id)
        return sgr

    @db_api.retry_if_session_inactive()
    def delete_security_group_rule(self, context, id):
        kwargs = {
            'context': context,
            'security_group_rule_id': id
        }
        self._registry_notify(resources.SECURITY_GROUP_RULE,
                              events.BEFORE_DELETE, id=id,
                              exc_cls=ext_sg.SecurityGroupRuleInUse, **kwargs)

        with context.session.begin(subtransactions=True):
            query = self._model_query(context,
                                      sg_models.SecurityGroupRule).filter(
                sg_models.SecurityGroupRule.id == id)

            self._registry_notify(resources.SECURITY_GROUP_RULE,
                                  events.PRECOMMIT_DELETE,
                                  exc_cls=ext_sg.SecurityGroupRuleInUse, id=id,
                                  **kwargs)

            try:
                sg_rule = query.one()
                # As there is a filter on a primary key it is not possible for
                # MultipleResultsFound to be raised
                context.session.delete(sg_rule)
            except exc.NoResultFound:
                raise ext_sg.SecurityGroupRuleNotFound(id=id)

            kwargs['security_group_id'] = sg_rule['security_group_id']

        registry.notify(
            resources.SECURITY_GROUP_RULE, events.AFTER_DELETE, self,
            **kwargs)

    def _extend_port_dict_security_group(self, port_res, port_db):
        # Security group bindings will be retrieved from the SQLAlchemy
        # model. As they're loaded eagerly with ports because of the
        # joined load they will not cause an extra query.
        security_group_ids = [sec_group_mapping['security_group_id'] for
                              sec_group_mapping in port_db.security_groups]
        port_res[ext_sg.SECURITYGROUPS] = security_group_ids
        return port_res

    # Register dict extend functions for ports
    db_base_plugin_v2.NeutronDbPluginV2.register_dict_extend_funcs(
        attributes.PORTS, ['_extend_port_dict_security_group'])

    def _process_port_create_security_group(self, context, port,
                                            security_group_ids):
        if validators.is_attr_set(security_group_ids):
            for security_group_id in security_group_ids:
                self._create_port_security_group_binding(context, port['id'],
                                                         security_group_id)
        # Convert to list as a set might be passed here and
        # this has to be serialized
        port[ext_sg.SECURITYGROUPS] = (security_group_ids and
                                       list(security_group_ids) or [])

    def _get_default_sg_id(self, context, tenant_id):
        try:
            query = self._model_query(context, sg_models.DefaultSecurityGroup)
            default_group = query.filter_by(tenant_id=tenant_id).one()
            return default_group['security_group_id']
        except exc.NoResultFound:
            pass

    @registry.receives(resources.PORT, [events.BEFORE_CREATE])
    @registry.receives(resources.NETWORK, [events.BEFORE_CREATE])
    def _ensure_default_security_group_handler(self, resource, event, trigger,
                                               context, **kwargs):
        tenant_id = kwargs[resource]['tenant_id']
        self._ensure_default_security_group(context, tenant_id)

    def _ensure_default_security_group(self, context, tenant_id):
        """Create a default security group if one doesn't exist.

        :returns: the default security group id for given tenant.
        """
        existing = self._get_default_sg_id(context, tenant_id)
        if existing is not None:
            return existing
        security_group = {
            'security_group':
                {'name': 'default',
                 'tenant_id': tenant_id,
                 'description': _('Default security group')}
        }
        return self.create_security_group(context, security_group,
                                          default_sg=True)['id']

    def _get_security_groups_on_port(self, context, port):
        """Check that all security groups on port belong to tenant.

        :returns: all security groups IDs on port belonging to tenant.
        """
        port = port['port']
        if not validators.is_attr_set(port.get(ext_sg.SECURITYGROUPS)):
            return
        if port.get('device_owner') and utils.is_port_trusted(port):
            return

        port_sg = port.get(ext_sg.SECURITYGROUPS, [])
        filters = {'id': port_sg}
        tenant_id = port.get('tenant_id')
        if tenant_id:
            filters['tenant_id'] = [tenant_id]
        valid_groups = set(g['id'] for g in
                           self.get_security_groups(context, fields=['id'],
                                                    filters=filters))

        requested_groups = set(port_sg)
        port_sg_missing = requested_groups - valid_groups
        if port_sg_missing:
            raise ext_sg.SecurityGroupNotFound(id=', '.join(port_sg_missing))

        return requested_groups

    def _ensure_default_security_group_on_port(self, context, port):
        # we don't apply security groups for dhcp, router
        port = port['port']
        if port.get('device_owner') and utils.is_port_trusted(port):
            return
        default_sg = self._ensure_default_security_group(context,
                                                         port['tenant_id'])
        if not validators.is_attr_set(port.get(ext_sg.SECURITYGROUPS)):
            port[ext_sg.SECURITYGROUPS] = [default_sg]

    def _check_update_deletes_security_groups(self, port):
        """Return True if port has as a security group and it's value
        is either [] or not is_attr_set, otherwise return False
        """
        if (ext_sg.SECURITYGROUPS in port['port'] and
            not (validators.is_attr_set(port['port'][ext_sg.SECURITYGROUPS])
                 and port['port'][ext_sg.SECURITYGROUPS] != [])):
            return True
        return False

    def _check_update_has_security_groups(self, port):
        """Return True if port has security_groups attribute set and
        its not empty, or False otherwise.
        This method is called both for port create and port update.
        """
        if (ext_sg.SECURITYGROUPS in port['port'] and
            (validators.is_attr_set(port['port'][ext_sg.SECURITYGROUPS]) and
             port['port'][ext_sg.SECURITYGROUPS] != [])):
            return True
        return False

    def update_security_group_on_port(self, context, id, port,
                                      original_port, updated_port):
        """Update security groups on port.

        This method returns a flag which indicates request notification
        is required and does not perform notification itself.
        It is because another changes for the port may require notification.
        """
        need_notify = False
        port_updates = port['port']
        if (ext_sg.SECURITYGROUPS in port_updates and
            not helpers.compare_elements(
                original_port.get(ext_sg.SECURITYGROUPS),
                port_updates[ext_sg.SECURITYGROUPS])):
            # delete the port binding and read it with the new rules
            port_updates[ext_sg.SECURITYGROUPS] = (
                self._get_security_groups_on_port(context, port))
            self._delete_port_security_group_bindings(context, id)
            self._process_port_create_security_group(
                context,
                updated_port,
                port_updates[ext_sg.SECURITYGROUPS])
            need_notify = True
        else:
            updated_port[ext_sg.SECURITYGROUPS] = (
                original_port[ext_sg.SECURITYGROUPS])
        return need_notify
