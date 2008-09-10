# Authors:
#   Jason Gerard DeRose <jderose@redhat.com>
#
# Copyright (C) 2008  Red Hat
# see file 'COPYING' for use and warranty information
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License as
# published by the Free Software Foundation; version 2 only
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA 02111-1307 USA

"""
Base classes for the public plugable.API instance, which the XML-RPC, CLI,
and UI all use.
"""

import re
import inspect
import plugable
from plugable import lock, check_name
import errors
from errors import check_type, check_isinstance, raise_TypeError
import ipa_types


RULE_FLAG = 'validation_rule'

def rule(obj):
    assert not hasattr(obj, RULE_FLAG)
    setattr(obj, RULE_FLAG, True)
    return obj

def is_rule(obj):
    return callable(obj) and getattr(obj, RULE_FLAG, False) is True


class DefaultFrom(plugable.ReadOnly):
    """
    Derives a default for one value using other supplied values.

    Here is an example that constructs a user's initials from his first
    and last name:

    >>> df = DefaultFrom(lambda f, l: f[0] + l[0], 'first', 'last')
    >>> df(first='John', last='Doe') # Both keys
    'JD'
    >>> df() is None # Returns None if any key is missing
    True
    >>> df(first='John', middle='Q') is None # Still returns None
    True
    """
    def __init__(self, callback, *keys):
        """
        :param callback: The callable to call when all ``keys`` are present.
        :param keys: The keys used to map from keyword to position arguments.
        """
        assert callable(callback), 'not a callable: %r' % callback
        assert len(keys) > 0, 'must have at least one key'
        for key in keys:
            assert type(key) is str, 'not an str: %r' % key
        self.callback = callback
        self.keys = keys
        lock(self)

    def __call__(self, **kw):
        """
        If all keys are present, calls the callback; otherwise returns None.

        :param kw: The keyword arguments.
        """
        vals = tuple(kw.get(k, None) for k in self.keys)
        if None in vals:
            return None
        try:
            return self.callback(*vals)
        except Exception:
            return None


class Option(plugable.ReadOnly):
    def __init__(self, name, type_,
            doc='',
            required=False,
            multivalue=False,
            default=None,
            default_from=None,
            rules=tuple(),
            normalize=None):
        self.name = check_name(name)
        self.doc = check_type(doc, str, 'doc')
        self.type = check_isinstance(type_, ipa_types.Type, 'type_')
        self.required = check_type(required, bool, 'required')
        self.multivalue = check_type(multivalue, bool, 'multivalue')
        self.default = default
        self.default_from = check_type(default_from,
            DefaultFrom, 'default_from', allow_none=True)
        self.__normalize = normalize
        self.rules = (type_.validate,) + rules
        lock(self)

    def __convert_scalar(self, value, index=None):
        if value is None:
            raise TypeError('value cannot be None')
        converted = self.type(value)
        if converted is None:
            raise errors.ConversionError(
                self.name, value, self.type, index=index
            )
        return converted

    def convert(self, value):
        if self.multivalue:
            if type(value) in (tuple, list):
                return tuple(
                    self.__convert_scalar(v, i) for (i, v) in enumerate(value)
                )
            return (self.__convert_scalar(value, 0),) # tuple
        return self.__convert_scalar(value)

    def __normalize_scalar(self, value):
        if not isinstance(value, basestring):
            raise_TypeError(value, basestring, 'value')
        try:
            return self.__normalize(value)
        except Exception:
            return value

    def normalize(self, value):
        if self.__normalize is None:
            return value
        if self.multivalue:
            if type(value) in (tuple, list):
                return tuple(self.__normalize_scalar(v) for v in value)
            return (self.__normalize_scalar(value),) # tuple
        return self.__normalize_scalar(value)

    def __validate_scalar(self, value, index=None):
        if type(value) is not self.type.type:
            raise_TypeError(value, self.type.type, 'value')
        for rule in self.rules:
            error = rule(value)
            if error is not None:
                raise errors.RuleError(
                    self.name, value, error, rule, index=index
                )

    def validate(self, value):
        if self.multivalue:
            if type(value) is not tuple:
                raise_TypeError(value, tuple, 'value')
            for (i, v) in enumerate(value):
                self.__validate_scalar(v, i)
        else:
            self.__validate_scalar(value)

    def get_default(self, **kw):
        if self.default_from is not None:
            default = self.default_from(**kw)
            if default is not None:
                try:
                    return self.convert(self.normalize(default))
                except errors.ValidationError:
                    return None
        return self.default

    def get_values(self):
        if self.type.name in ('Enum', 'CallbackEnum'):
            return self.type.values
        return tuple()

    def __call__(self, value, **kw):
        if value in ('', tuple(), []):
            value = None
        if value is None:
            value = self.get_default(**kw)
        if value is None:
            if self.required:
                raise errors.RequirementError(self.name)
            return None
        else:
            value = self.convert(self.normalize(value))
            self.validate(value)
            return value

    def __repr__(self):
        return '%s(%r, %s())' % (
            self.__class__.__name__,
            self.name,
            self.type.name,
        )


class Command(plugable.Plugin):
    __public__ = frozenset((
        'get_default',
        'convert',
        'normalize',
        'validate',
        'execute',
        '__call__',
        'smart_option_order',
        'Option',
        'takes_args',
    ))
    __Option = None
    options = tuple()
    takes_args = tuple()

    def __init__(self):
        self.args = plugable.NameSpace(self.__check_args(), sort=False)

    def get_args(self):
        return self.takes_args

    def get_options(self):
        return self.options

    def __check_args(self):
        optional = False
        multivalue = False
        for arg in self.get_args():
            if type(arg) is str:
                arg = Option(arg, ipa_types.Unicode(), required=True)
            elif not isinstance(arg, Option):
                raise TypeError(
                    'arg: need %r or %r; got %r' % (str, Option, arg)
                )
            if optional and arg.required:
                raise ValueError(
                    '%s: required argument after optional' % arg.name
                )
            if multivalue:
                raise ValueError(
                    '%s: only final argument can be multivalue' % arg.name
                )
            if not arg.required:
                optional = True
            if arg.multivalue:
                multivalue = True
            yield arg

    def __get_Option(self):
        """
        Returns the NameSpace containing the Option instances.
        """
        if self.__Option is None:
            object.__setattr__(self, '_Command__Option',
                plugable.NameSpace(self.get_options()),
            )
        return self.__Option
    Option = property(__get_Option)

    def __convert_iter(self, kw):
        for (key, value) in kw.iteritems():
            if key in self.Option:
                yield (key, self.Option[key].convert(value))
            else:
                yield (key, value)

    def convert(self, **kw):
        return dict(self.__convert_iter(kw))

    def __normalize_iter(self, kw):
        for (key, value) in kw.iteritems():
            if key in self.Option:
                yield (key, self.Option[key].normalize(value))
            else:
                yield (key, value)

    def normalize(self, **kw):
        return dict(self.__normalize_iter(kw))

    def __get_default_iter(self, kw):
        for option in self.Option():
            if option.name not in kw:
                value = option.get_default(**kw)
                if value is not None:
                    yield(option.name, value)

    def get_default(self, **kw):
        self.print_call('default', kw, 1)
        return dict(self.__get_default_iter(kw))

    def validate(self, **kw):
        self.print_call('validate', kw, 1)
        for option in self.Option():
            value = kw.get(option.name, None)
            if value is not None:
                option.validate(value)
            elif option.required:
                raise errors.RequirementError(option.name)

    def execute(self, **kw):
        self.print_call('execute', kw, 1)
        pass

    def print_call(self, method, kw, tab=0):
        print '%s%s.%s(%s)\n' % (
            ' ' * (tab *2),
            self.name,
            method,
            ', '.join('%s=%r' % (k, kw[k]) for k in sorted(kw)),
        )

    def __call__(self, *args, **kw):
        print ''
        self.print_call('__call__', kw)
        kw = self.normalize(**kw)
        kw.update(self.get_default(**kw))
        self.validate(**kw)
        self.execute(**kw)

    def smart_option_order(self):
        def get_key(option):
            if option.required:
                if option.default_from is None:
                    return 0
                return 1
            return 2
        for option in sorted(self.Option(), key=get_key):
            yield option



class Object(plugable.Plugin):
    __public__ = frozenset((
        'Method',
        'Property',
    ))
    __Method = None
    __Property = None

    def __get_Method(self):
        return self.__Method
    Method = property(__get_Method)

    def __get_Property(self):
        return self.__Property
    Property = property(__get_Property)

    def finalize(self, api):
        super(Object, self).finalize(api)
        self.__Method = self.__create_namespace('Method')
        self.__Property = self.__create_namespace('Property')

    def __create_namespace(self, name):
        return plugable.NameSpace(self.__filter_members(name))

    def __filter_members(self, name):
        namespace = getattr(self.api, name)
        assert type(namespace) is plugable.NameSpace
        for proxy in namespace(): # Equivalent to dict.itervalues()
            if proxy.obj_name == self.name:
                yield proxy.__clone__('attr_name')


class Attribute(plugable.Plugin):
    __public__ = frozenset((
        'obj',
        'obj_name',
    ))
    __obj = None

    def __init__(self):
        m = re.match(
            '^([a-z][a-z0-9]+)_([a-z][a-z0-9]+)$',
            self.__class__.__name__
        )
        assert m
        self.__obj_name = m.group(1)
        self.__attr_name = m.group(2)

    def __get_obj_name(self):
        return self.__obj_name
    obj_name = property(__get_obj_name)

    def __get_attr_name(self):
        return self.__attr_name
    attr_name = property(__get_attr_name)

    def __get_obj(self):
        """
        Returns the obj instance this attribute is associated with, or None
        if no association has been set.
        """
        return self.__obj
    obj = property(__get_obj)

    def finalize(self, api):
        super(Attribute, self).finalize(api)
        self.__obj = api.Object[self.obj_name]


class Method(Attribute, Command):
    __public__ = Attribute.__public__.union(Command.__public__)

    def get_options(self):
        for option in self.options:
            yield option
        if self.obj is not None and self.obj.Property is not None:
            for proxy in self.obj.Property():
                yield proxy.option


class Property(Attribute):
    __public__ = frozenset((
        'rules',
        'option',
        'type',
    )).union(Attribute.__public__)

    type = ipa_types.Unicode()
    required = False
    multivalue = False
    default = None
    default_from = None
    normalize = None

    def __init__(self):
        super(Property, self).__init__()
        self.rules = tuple(sorted(
            self.__rules_iter(),
            key=lambda f: getattr(f, '__name__'),
        ))
        self.option = Option(self.attr_name, self.type,
            doc=self.doc,
            required=self.required,
            multivalue=self.multivalue,
            default=self.default,
            default_from=self.default_from,
            rules=self.rules,
            normalize=self.normalize,
        )

    def __rules_iter(self):
        """
        Iterates through the attributes in this instance to retrieve the
        methods implementing validation rules.
        """
        for name in dir(self.__class__):
            if name.startswith('_'):
                continue
            base_attr = getattr(self.__class__, name)
            if is_rule(base_attr):
                attr = getattr(self, name)
                if is_rule(attr):
                    yield attr


class Application(Command):
    """
    Base class for commands register by an external application.

    Special commands that only apply to a particular application built atop
    `ipalib` should subclass from ``Application``.

    Because ``Application`` subclasses from `Command`, plugins that subclass
    from ``Application`` with be available in both the ``api.Command`` and
    ``api.Application`` namespaces.
    """

    __public__ = frozenset((
        'application',
        'set_application'
    )).union(Command.__public__)
    __application = None

    def __get_application(self):
        """
        Returns external ``application`` object.
        """
        return self.__application
    application = property(__get_application)

    def set_application(self, application):
        """
        Sets the external application object to ``application``.
        """
        if self.__application is not None:
            raise AttributeError(
                '%s.application can only be set once' % self.name
            )
        if application is None:
            raise TypeError(
                '%s.application cannot be None' % self.name
            )
        object.__setattr__(self, '_Application__application', application)
        assert self.application is application
