#!/usr/bin/env python3

#
#   This file is part of m.css.
#
#   Copyright © 2017, 2018, 2019 Vladimír Vondruš <mosra@centrum.cz>
#
#   Permission is hereby granted, free of charge, to any person obtaining a
#   copy of this software and associated documentation files (the "Software"),
#   to deal in the Software without restriction, including without limitation
#   the rights to use, copy, modify, merge, publish, distribute, sublicense,
#   and/or sell copies of the Software, and to permit persons to whom the
#   Software is furnished to do so, subject to the following conditions:
#
#   The above copyright notice and this permission notice shall be included
#   in all copies or substantial portions of the Software.
#
#   THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
#   IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
#   FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
#   THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
#   LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
#   FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
#   DEALINGS IN THE SOFTWARE.
#

import argparse
import copy
import docutils
import enum
import urllib.parse
import html
import importlib
import inspect
import logging
import mimetypes
import os
import re
import sys
import shutil

from types import SimpleNamespace as Empty
from importlib.machinery import SourceFileLoader
from typing import Tuple, Dict, Set, Any, List
from urllib.parse import urljoin
from distutils.version import LooseVersion

import jinja2

sys.path.append(os.path.join(os.path.dirname(os.path.realpath(__file__)), '../plugins'))
import m.htmlsanity

default_templates = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'templates/python/')

default_config = {
    'PROJECT_TITLE': 'My Python Project',
    'PROJECT_SUBTITLE': None,
    'MAIN_PROJECT_URL': None,
    'INPUT': None,
    'OUTPUT': 'output',
    'INPUT_MODULES': [],
    'INPUT_PAGES': [],
    'INPUT_DOCS': [],
    'OUTPUT': 'output',
    'THEME_COLOR': '#22272e',
    'FAVICON': 'favicon-dark.png',
    'STYLESHEETS': [
        'https://fonts.googleapis.com/css?family=Source+Sans+Pro:400,400i,600,600i%7CSource+Code+Pro:400,400i,600',
        '../css/m-dark+documentation.compiled.css'],
    'EXTRA_FILES': [],
    'LINKS_NAVBAR1': [
        ('Pages', 'pages', []),
        ('Modules', 'modules', []),
        ('Classes', 'classes', [])],
    'LINKS_NAVBAR2': [],

    'PAGE_HEADER': None,
    'FINE_PRINT': '[default]',
    'FORMATTED_METADATA': ['summary'],

    'PLUGINS': [],
    'PLUGIN_PATHS': [],

    'CLASS_INDEX_EXPAND_LEVELS': 1,
    'CLASS_INDEX_EXPAND_INNER': False,

    'PYBIND11_COMPATIBILITY': False,

    'SEARCH_DISABLED': False,
    'SEARCH_DOWNLOAD_BINARY': False,
    'SEARCH_HELP': """.. raw:: html

    <p class="m-noindent">Search for modules, classes, functions and other
    symbols. You can omit any prefix from the symbol path; adding a <code>.</code>
    suffix lists all members of given symbol.</p>
    <p class="m-noindent">Use <span class="m-label m-dim">&darr;</span>
    / <span class="m-label m-dim">&uarr;</span> to navigate through the list,
    <span class="m-label m-dim">Enter</span> to go.
    <span class="m-label m-dim">Tab</span> autocompletes common prefix, you can
    copy a link to the result using <span class="m-label m-dim">⌘</span>
    <span class="m-label m-dim">L</span> while <span class="m-label m-dim">⌘</span>
    <span class="m-label m-dim">M</span> produces a Markdown link.</p>
""",
    'SEARCH_BASE_URL': None,
    'SEARCH_EXTERNAL_URL': None,
}

class IndexEntry:
    def __init__(self):
        self.kind: str
        self.name: str
        self.url: str
        self.summary: str
        self.has_nestaable_children: bool = False
        self.children: List[IndexEntry] = []

class State:
    def __init__(self, config):
        self.config = config
        self.class_index: List[IndexEntry] = []
        self.page_index: List[IndexEntry] = []
        self.module_mapping: Dict[str, str] = {}
        self.module_docs: Dict[str, Dict[str, str]] = {}
        self.class_docs: Dict[str, Dict[str, str]] = {}
        self.data_docs: Dict[str, Dict[str, str]] = {}
        self.external_data: Set[str] = set()

        self.hooks_pre_page: List = []
        self.hooks_post_run: List = []

def is_internal_function_name(name: str) -> bool:
    """If the function name is internal.

    Skips underscored functions but keeps special functions such as __init__.
    """
    return name.startswith('_') and not (name.startswith('__') and name.endswith('__'))

def map_name_prefix(state: State, type: str) -> str:
    for prefix, replace in state.module_mapping.items():
        if type == prefix or type.startswith(prefix + '.'):
            return replace + type[len(prefix):]

    # No mapping found, return the type as-is
    return type

def is_internal_or_imported_module_member(state: State, parent, path: str, name: str, object) -> bool:
    """If the module member is internal or imported."""

    if name.startswith('_'): return True

    # If this is not a module, check if the enclosing module of the object is
    # what expected. If not, it's a class/function/... imported from elsewhere
    # and we don't want those.
    # TODO: xml.dom.domreg says the things from it should be imported as
    #   xml.dom.foo() and this check discards them, can it be done without
    #   manually adding __all__?
    if not inspect.ismodule(object):
        # Variables don't have the __module__ attribute, so check for its
        # presence. Right now *any* variable will be present in the output, as
        # there is no way to check where it comes from.
        if hasattr(object, '__module__') and map_name_prefix(state, object.__module__) != '.'.join(path):
            return True

    # If this is a module, then things get complicated again and we need to
    # handle modules and packages differently. See also for more info:
    # https://stackoverflow.com/a/7948672
    else:
        # pybind11 submodules have __package__ set to None (instead of '') for
        # nested modules. Allow these. The parent's __package__ can be None (if
        # it's a nested submodule), '' (if it's a top-level module) or a string
        # (if the parent is a Python package), can't really check further.
        if state.config['PYBIND11_COMPATIBILITY'] and object.__package__ is None: return False

        # The parent is a single-file module (not a package), these don't have
        # submodules so this is most definitely an imported module. Source:
        # https://docs.python.org/3/reference/import.html#packages
        if not parent.__package__: return True

        # The parent is a package and this is either a submodule or a
        # subpackage. Check that the __package__ of parent and child is either
        # the same or it's parent + child name
        if object.__package__ not in [parent.__package__, parent.__package__ + '.' + name]: return True

    # If nothing of the above matched, then it's a thing we want to document
    return False

def is_enum(state: State, object) -> bool:
    return (inspect.isclass(object) and issubclass(object, enum.Enum)) or (state.config['PYBIND11_COMPATIBILITY'] and hasattr(object, '__members__'))

def make_url(path: List[str]) -> str:
    return '.'.join(path) + '.html'

_pybind_name_rx = re.compile('[a-zA-Z0-9_]*')
_pybind_arg_name_rx = re.compile('[*a-zA-Z0-9_]+')
_pybind_type_rx = re.compile('[a-zA-Z0-9_.]+')
_pybind_default_value_rx = re.compile('[^,)]+')

def parse_pybind_type(state: State, signature: str) -> str:
    input_type = _pybind_type_rx.match(signature).group(0)
    signature = signature[len(input_type):]
    type = map_name_prefix(state, input_type)
    if signature and signature[0] == '[':
        type += '['
        signature = signature[1:]
        while signature[0] != ']':
            signature, inner_type = parse_pybind_type(state, signature)
            type += inner_type

            if signature[0] == ']': break
            assert signature.startswith(', ')
            signature = signature[2:]
            type += ', '

        assert signature[0] == ']'
        signature = signature[1:]
        type += ']'

    return signature, type

def parse_pybind_signature(state: State, signature: str) -> Tuple[str, str, List[Tuple[str, str, str]], str]:
    original_signature = signature # For error reporting
    name = _pybind_name_rx.match(signature).group(0)
    signature = signature[len(name):]
    args = []
    assert signature[0] == '('
    signature = signature[1:]

    # Arguments
    while signature[0] != ')':
        # Name
        arg_name = _pybind_arg_name_rx.match(signature).group(0)
        assert arg_name
        signature = signature[len(arg_name):]

        # Type (optional)
        if signature.startswith(': '):
            signature = signature[2:]
            signature, arg_type = parse_pybind_type(state, signature)
        else:
            arg_type = None

        # Default (optional) -- for now take everything until the next comma
        # TODO: ugh, do properly
        if signature.startswith('='):
            signature = signature[1:]
            default = _pybind_default_value_rx.match(signature).group(0)
            signature = signature[len(default):]
        else:
            default = None

        args += [(arg_name, arg_type, default)]

        if signature[0] == ')': break

        # Failed to parse, return an ellipsis and docs
        if not signature.startswith(', '):
            end = original_signature.find('\n')
            logging.warning("cannot parse pybind11 function signature %s", original_signature[:end if end != -1 else None])
            if end != -1 and len(original_signature) > end + 1 and original_signature[end + 1] == '\n':
                summary = extract_summary(state, {}, [], original_signature[end + 1:])
            else:
                summary = ''
            return (name, summary, [('…', None, None)], None)

        signature = signature[2:]

    assert signature[0] == ')'
    signature = signature[1:]

    # Return type (optional)
    if signature.startswith(' -> '):
        signature = signature[4:]
        signature, return_type = parse_pybind_type(state, signature)
    else:
        return_type = None

    if signature and signature[0] != '\n':
        end = original_signature.find('\n')
        logging.warning("cannot parse pybind11 function signature %s", original_signature[:end if end != -1 else None])
        if end != -1 and len(original_signature) > end + 1 and original_signature[end + 1] == '\n':
            summary = extract_summary(state, {}, [], original_signature[end + 1:])
        else:
            summary = ''
        return (name, summary, [('…', None, None)], None)

    if len(signature) > 1 and signature[1] == '\n':
        summary = extract_summary(state, {}, [], signature[2:])
    else:
        summary = ''

    return (name, summary, args, return_type)

def parse_pybind_docstring(state: State, name: str, doc: str) -> List[Tuple[str, str, List[Tuple[str, str, str]], str]]:
    # Multiple overloads, parse each separately
    overload_header = "{}(*args, **kwargs)\nOverloaded function.\n\n".format(name);
    if doc.startswith(overload_header):
        doc = doc[len(overload_header):]
        overloads = []
        id = 1
        while True:
            assert doc.startswith('{}. {}('.format(id, name))
            id = id + 1
            next = doc.find('{}. {}('.format(id, name))

            # Parse the signature and docs from known slice
            overloads += [parse_pybind_signature(state, doc[len(str(id - 1)) + 2:next])]
            assert overloads[-1][0] == name
            if next == -1: break

            # Continue to the next signature
            doc = doc[next:]

        return overloads

    # Normal function, parse and return the first signature
    else:
        return [parse_pybind_signature(state, doc)]

def extract_summary(state: State, external_docs, path: List[str], doc: str) -> str:
    # Prefer external docs, if available
    path_str = '.'.join(path)
    if path_str in external_docs and external_docs[path_str]['summary']:
        return render_inline_rst(state, external_docs[path_str]['summary'])

    if not doc: return '' # some modules (xml.etree) have that :(
    doc = inspect.cleandoc(doc)
    end = doc.find('\n\n')
    return html.escape(doc if end == -1 else doc[:end])

def extract_type(type) -> str:
    # For types we concatenate the type name with its module unless it's
    # builtins (i.e., we want re.Match but not builtins.int).
    return (type.__module__ + '.' if type.__module__ != 'builtins' else '') + type.__name__

def extract_annotation(state: State, annotation) -> str:
    # TODO: why this is not None directly?
    if annotation is inspect.Signature.empty: return None

    # Annotations can be strings, also https://stackoverflow.com/a/33533514
    if type(annotation) == str: return map_name_prefix(state, annotation)

    # To avoid getting <class 'foo.bar'> for types (and getting foo.bar
    # instead) but getting the actual type for types annotated with e.g.
    # List[int], we need to check if the annotation is actually from the
    # typing module or it's directly a type. In Python 3.7 this worked with
    # inspect.isclass(annotation), but on 3.6 that gives True for annotations
    # as well and then we would get just List instead of List[int].
    if annotation.__module__ == 'typing': return map_name_prefix(state, str(annotation))
    return map_name_prefix(state, extract_type(annotation))

def render(config, template: str, page, env: jinja2.Environment):
    template = env.get_template(template)
    rendered = template.render(page=page, FILENAME=page.url, **config)
    with open(os.path.join(config['OUTPUT'], page.url), 'wb') as f:
        f.write(rendered.encode('utf-8'))
        # Add back a trailing newline so we don't need to bother with
        # patching test files to include a trailing newline to make Git
        # happy
        # TODO could keep_trailing_newline fix this better?
        f.write(b'\n')

def extract_module_doc(state: State, path: List[str], module):
    assert inspect.ismodule(module)

    out = Empty()
    out.url = make_url(path)
    out.name = path[-1]
    out.summary = extract_summary(state, state.class_docs, path, module.__doc__)
    return out

def extract_class_doc(state: State, path: List[str], class_):
    assert inspect.isclass(class_)

    out = Empty()
    out.url = make_url(path)
    out.name = path[-1]
    out.summary = extract_summary(state, state.class_docs, path, class_.__doc__)
    return out

def extract_enum_doc(state: State, path: List[str], enum_):
    out = Empty()
    out.name = path[-1]
    out.values = []
    out.has_details = False
    out.has_value_details = False

    # The happy case
    if issubclass(enum_, enum.Enum):
        # Enum doc is by default set to a generic value. That's useless as well.
        if enum_.__doc__ == 'An enumeration.':
            out.summary = ''
        else:
            # TODO: external summary for enums
            out.summary = extract_summary(state, {}, [], enum_.__doc__)

        out.base = extract_type(enum_.__base__)

        for i in enum_:
            value = Empty()
            value.name = i.name
            value.value = html.escape(repr(i.value))

            # Value doc gets by default inherited from the enum, that's useless
            if i.__doc__ == enum_.__doc__:
                value.summary = ''
            else:
                # TODO: external summary for enum values
                value.summary = extract_summary(state, {}, [], i.__doc__)

            if value.summary:
                out.has_details = True
                out.has_value_details = True
            out.values += [value]

    # Pybind11 enums are ... different
    elif state.config['PYBIND11_COMPATIBILITY']:
        assert hasattr(enum_, '__members__')

        # TODO: external summary for enums
        out.summary = extract_summary(state, {}, [], enum_.__doc__)
        out.base = None

        for name, v in enum_.__members__.items():
            value = Empty()
            value. name = name
            value.value = int(v)
            # TODO: once https://github.com/pybind/pybind11/pull/1160 is
            #       released, extract from class docs (until then the class
            #       docstring is duplicated here, which is useless)
            value.summary = ''
            out.values += [value]

    return out

def extract_function_doc(state: State, parent, path: List[str], function) -> List[Any]:
    assert inspect.isfunction(function) or inspect.ismethod(function) or inspect.isroutine(function)

    # Extract the signature from the docstring for pybind11, since it can't
    # expose it to the metadata: https://github.com/pybind/pybind11/issues/990
    # What's not solvable with metadata, however, are function overloads ---
    # one function in Python may equal more than one function on the C++ side.
    # To make the docs usable, list all overloads separately.
    if state.config['PYBIND11_COMPATIBILITY'] and function.__doc__.startswith(path[-1]):
        funcs = parse_pybind_docstring(state, path[-1], function.__doc__)
        overloads = []
        for name, summary, args, type in funcs:
            out = Empty()
            out.name = path[-1]
            out.params = []
            out.has_complex_params = False
            out.has_details = False
            # TODO: external summary for functions
            out.summary = summary

            # Don't show None return type for void functions
            out.type = None if type == 'None' else type

            # There's no other way to check staticmethods than to check for
            # self being the name of first parameter :( No support for
            # classmethods, as C++11 doesn't have that
            out.is_classmethod = False
            if inspect.isclass(parent) and args and args[0][0] == 'self':
                out.is_staticmethod = False
            else:
                out.is_staticmethod = True

            # Guesstimate whether the arguments are positional-only or
            # position-or-keyword. It's either all or none. This is a brown
            # magic, sorry.

            # For instance methods positional-only argument names are either
            # self (for the first argument) or arg(I-1) (for second
            # argument and further). Also, the `self` argument is
            # positional-or-keyword only if there are positional-or-keyword
            # arguments afgter it, otherwise it's positional-only.
            if inspect.isclass(parent) and not out.is_staticmethod:
                assert args and args[0][0] == 'self'

                positional_only = True
                for i, arg in enumerate(args[1:]):
                    name, type, default = arg
                    if name != 'arg{}'.format(i):
                        positional_only = False
                        break

            # For static methods or free functions positional-only arguments
            # are argI.
            else:
                positional_only = True
                for i, arg in enumerate(args):
                    name, type, default = arg
                    if name != 'arg{}'.format(i):
                        positional_only = False
                        break

            for i, arg in enumerate(args):
                name, type, default = arg
                param = Empty()
                param.name = name
                # Don't include redundant type for the self argument
                if name == 'self': param.type = None
                else: param.type = type
                param.default = html.escape(default or '')
                if type or default: out.has_complex_params = True

                # *args / **kwargs can still appear in the parsed signatures if
                # the function accepts py::args / py::kwargs directly
                if name == '*args':
                    param.name = 'args'
                    param.kind = 'VAR_POSITIONAL'
                elif name == '**kwargs':
                    param.name = 'kwargs'
                    param.kind = 'VAR_KEYWORD'
                else:
                    param.kind = 'POSITIONAL_ONLY' if positional_only else 'POSITIONAL_OR_KEYWORD'

                out.params += [param]

            overloads += [out]

        return overloads

    # Sane introspection path for non-pybind11 code
    else:
        out = Empty()
        out.name = path[-1]
        out.params = []
        out.has_complex_params = False
        out.has_details = False
        # TODO: external summary for functions
        out.summary = extract_summary(state, {}, [], function.__doc__)

        # Decide if classmethod or staticmethod in case this is a method
        if inspect.isclass(parent):
            out.is_classmethod = inspect.ismethod(function)
            out.is_staticmethod = out.name in parent.__dict__ and isinstance(parent.__dict__[out.name], staticmethod)

        try:
            signature = inspect.signature(function)
            out.type = extract_annotation(state, signature.return_annotation)
            for i in signature.parameters.values():
                param = Empty()
                param.name = i.name
                param.type = extract_annotation(state, i.annotation)
                if param.type:
                    out.has_complex_params = True
                if i.default is inspect.Signature.empty:
                    param.default = None
                else:
                    param.default = repr(i.default)
                    out.has_complex_params = True
                param.kind = str(i.kind)
                out.params += [param]

        # In CPython, some builtin functions (such as math.log) do not provide
        # metadata about their arguments. Source:
        # https://docs.python.org/3/library/inspect.html#inspect.signature
        except ValueError:
            param = Empty()
            param.name = '...'
            param.name_type = param.name
            out.params = [param]
            out.type = None

        return [out]

def extract_property_doc(state: State, path: List[str], property):
    assert inspect.isdatadescriptor(property)

    out = Empty()
    out.name = path[-1]
    # TODO: external summary for properties
    out.summary = extract_summary(state, {}, [], property.__doc__)
    out.is_settable = property.fset is not None
    out.is_deletable = property.fdel is not None
    out.has_details = False

    try:
        signature = inspect.signature(property.fget)
        out.type = extract_annotation(state, signature.return_annotation)
    except ValueError:
        # pybind11 properties have the type in the docstring
        if state.config['PYBIND11_COMPATIBILITY']:
            out.type = parse_pybind_signature(state, property.fget.__doc__)[3]
        else:
            out.type = None

    return out

def extract_data_doc(state: State, parent, path: List[str], data):
    assert not inspect.ismodule(data) and not inspect.isclass(data) and not inspect.isroutine(data) and not inspect.isframe(data) and not inspect.istraceback(data) and not inspect.iscode(data)

    out = Empty()
    out.name = path[-1]
    # Welp. https://stackoverflow.com/questions/8820276/docstring-for-variable
    out.summary = ''
    out.has_details = False
    if hasattr(parent, '__annotations__') and out.name in parent.__annotations__:
        out.type = extract_annotation(state, parent.__annotations__[out.name])
    else:
        out.type = None
    # The autogenerated <foo.bar at 0xbadbeef> is useless, so provide the value
    # only if __repr__ is implemented for given type
    if '__repr__' in type(data).__dict__:
        out.value = html.escape(repr(data))
    else:
        out.value = None

    # External data summary, if provided
    path_str = '.'.join(path)
    if path_str in state.data_docs:
        # TODO: use also the contents
        out.summary = render_inline_rst(state, state.data_docs[path_str]['summary'])
        del state.data_docs[path_str]

    return out

def render_module(state: State, path, module, env):
    logging.debug("generating %s.html", '.'.join(path))

    # Call all registered page begin hooks
    for hook in state.hooks_pre_page: hook()

    url_base = ''
    breadcrumb = []
    for i in path:
        url_base += i + '.'
        breadcrumb += [(i, url_base + 'html')]

    page = Empty()
    page.summary = extract_summary(state, state.module_docs, path, module.__doc__)
    page.url = breadcrumb[-1][1]
    page.breadcrumb = breadcrumb
    page.prefix_wbr = '.<wbr />'.join(path + [''])
    page.modules = []
    page.classes = []
    page.enums = []
    page.functions = []
    page.data = []
    page.has_enum_details = False

    # External page content, if provided
    path_str = '.'.join(path)
    if path_str in state.module_docs:
        page.content = render_rst(state, state.module_docs[path_str]['content'])
        state.module_docs[path_str]['used'] = True

    # Index entry for this module, returned together with children at the end
    index_entry = IndexEntry()
    index_entry.kind = 'module'
    index_entry.name = breadcrumb[-1][0]
    index_entry.url = page.url
    index_entry.summary = page.summary

    # List of inner modules and classes to render, these will be done after the
    # current class introspection is done to have some better memory allocation
    # pattern
    modules_to_render = []
    classes_to_render = []

    # This is actually complicated -- if the module defines __all__, use that.
    # The __all__ is meant to expose the public API, so we don't filter out
    # underscored things.
    if hasattr(module, '__all__'):
        # Names exposed in __all__ could be also imported from elsewhere, for
        # example this is a common pattern with native libraries and we want
        # Foo, Bar, submodule and *everything* in submodule to be referred to
        # as `library.RealName` (`library.submodule.func()`, etc.) instead of
        # `library._native.Foo`, `library._native.sub.func()` etc.
        #
        #   from ._native import Foo as PublicName
        #   from ._native import sub as submodule
        #   __all__ = ['PublicName', 'submodule']
        #
        # The name references can be cyclic so extract the mapping in a
        # separate pass before everything else.
        for name in module.__all__:
            # Everything available in __all__ is already imported, so get those
            # directly
            object = getattr(module, name)
            subpath = path + [name]

            # Modules have __name__ while other objects have __module__, need
            # to check both.
            if inspect.ismodule(object) and object.__name__ != '.'.join(subpath):
                assert object.__name__ not in state.module_mapping
                state.module_mapping[object.__name__] = '.'.join(subpath)
            elif hasattr(object, '__module__'):
                subname = object.__module__ + '.' + object.__name__
                if subname != '.'.join(subpath):
                    assert subname not in state.module_mapping
                    state.module_mapping[subname] = '.'.join(subpath)

        # Now extract the actual docs
        for name in module.__all__:
            object = getattr(module, name)
            subpath = path + [name]

            # We allow undocumented submodules (since they're often in the
            # standard lib), but not undocumented classes etc. Render the
            # submodules and subclasses recursively.
            if inspect.ismodule(object):
                page.modules += [extract_module_doc(state, subpath, object)]
                index_entry.children += [render_module(state, subpath, object, env)]
            elif inspect.isclass(object) and not is_enum(state, object):
                page.classes += [extract_class_doc(state, subpath, object)]
                index_entry.children += [render_class(state, subpath, object, env)]
            elif inspect.isclass(object) and is_enum(state, object):
                enum_ = extract_enum_doc(state, subpath, object)
                page.enums += [enum_]
                if enum_.has_details: page.has_enum_details = True
            elif inspect.isfunction(object) or inspect.isbuiltin(object):
                page.functions += extract_function_doc(state, module, subpath, object)
            # Assume everything else is data. The builtin help help() (from
            # pydoc) does the same:
            # https://github.com/python/cpython/blob/d29b3dd9227cfc4a23f77e99d62e20e063272de1/Lib/pydoc.py#L113
            # TODO: unify this query
            elif not inspect.isframe(object) and not inspect.istraceback(object) and not inspect.iscode(object):
                page.data += [extract_data_doc(state, module, subpath, object)]
            else: # pragma: no cover
                logging.warning("unknown symbol %s in %s", name, '.'.join(path))

    # Otherwise, enumerate the members using inspect. However, inspect lists
    # also imported modules, functions and classes, so take only those which
    # have __module__ equivalent to `path`.
    else:
        # Get (and render) inner modules
        for name, object in inspect.getmembers(module, inspect.ismodule):
            if is_internal_or_imported_module_member(state, module, path, name, object): continue

            subpath = path + [name]
            page.modules += [extract_module_doc(state, subpath, object)]
            modules_to_render += [(subpath, object)]

        # Get (and render) inner classes
        for name, object in inspect.getmembers(module, lambda o: inspect.isclass(o) and not is_enum(state, o)):
            if is_internal_or_imported_module_member(state, module, path, name, object): continue

            subpath = path + [name]
            if not object.__doc__: logging.warning("%s is undocumented", '.'.join(subpath))

            page.classes += [extract_class_doc(state, subpath, object)]
            classes_to_render += [(subpath, object)]

        # Get enums
        for name, object in inspect.getmembers(module, lambda o: is_enum(state, o)):
            if is_internal_or_imported_module_member(state, module, path, name, object): continue

            subpath = path + [name]
            if not object.__doc__: logging.warning("%s is undocumented", '.'.join(subpath))

            enum_ = extract_enum_doc(state, subpath, object)
            page.enums += [enum_]
            if enum_.has_details: page.has_enum_details = True

        # Get inner functions
        for name, object in inspect.getmembers(module, lambda o: inspect.isfunction(o) or inspect.isbuiltin(o)):
            if is_internal_or_imported_module_member(state, module, path, name, object): continue

            subpath = path + [name]
            if not object.__doc__: logging.warning("%s() is undocumented", '.'.join(subpath))

            page.functions += extract_function_doc(state, module, subpath, object)

        # Get data
        # TODO: unify this query
        for name, object in inspect.getmembers(module, lambda o: not inspect.ismodule(o) and not inspect.isclass(o) and not inspect.isroutine(o) and not inspect.isframe(o) and not inspect.istraceback(o) and not inspect.iscode(o)):
            if is_internal_or_imported_module_member(state, module, path, name, object): continue

            page.data += [extract_data_doc(state, module, path + [name], object)]

    # Render the module, free the page data to avoid memory rising indefinitely
    render(state.config, 'module.html', page, env)
    del page

    # Render submodules and subclasses
    for subpath, object in modules_to_render:
        index_entry.children += [render_module(state, subpath, object, env)]
    for subpath, object in classes_to_render:
        index_entry.children += [render_class(state, subpath, object, env)]

    return index_entry

# Builtin dunder functions have hardcoded docstrings. This is totally useless
# to have in the docs, so filter them out. Uh... kinda ugly.
_filtered_builtin_functions = set([
    ('__delattr__', "Implement delattr(self, name)."),
    ('__eq__', "Return self==value."),
    ('__ge__', "Return self>=value."),
    ('__getattribute__', "Return getattr(self, name)."),
    ('__gt__', "Return self>value."),
    ('__hash__', "Return hash(self)."),
    ('__init__', "Initialize self.  See help(type(self)) for accurate signature."),
    ('__init_subclass__',
        "This method is called when a class is subclassed.\n\n"
        "The default implementation does nothing. It may be\n"
        "overridden to extend subclasses.\n"),
    ('__le__', "Return self<=value."),
    ('__lt__', "Return self<value."),
    ('__ne__', "Return self!=value."),
    ('__new__',
        "Create and return a new object.  See help(type) for accurate signature."),
    ('__repr__', "Return repr(self)."),
    ('__setattr__', "Implement setattr(self, name, value)."),
    ('__str__', "Return str(self)."),
    ('__subclasshook__',
        "Abstract classes can override this to customize issubclass().\n\n"
        "This is invoked early on by abc.ABCMeta.__subclasscheck__().\n"
        "It should return True, False or NotImplemented.  If it returns\n"
        "NotImplemented, the normal algorithm is used.  Otherwise, it\n"
        "overrides the normal algorithm (and the outcome is cached).\n")
])

# Python 3.6 has slightly different docstrings than 3.7
if LooseVersion(sys.version) >= LooseVersion("3.7"):
    _filtered_builtin_functions.update({
        ('__dir__', "Default dir() implementation."),
        ('__format__', "Default object formatter."),
        ('__reduce__', "Helper for pickle."),
        ('__reduce_ex__', "Helper for pickle."),
        ('__sizeof__', "Size of object in memory, in bytes."),
    })
else:
    _filtered_builtin_functions.update({
        ('__dir__', "__dir__() -> list\ndefault dir() implementation"),
        ('__format__', "default object formatter"),
        ('__reduce__', "helper for pickle"),
        ('__reduce_ex__', "helper for pickle"),
        ('__sizeof__', "__sizeof__() -> int\nsize of object in memory, in bytes")
    })

_filtered_builtin_properties = set([
    ('__weakref__', "list of weak references to the object (if defined)")
])

def render_class(state: State, path, class_, env):
    logging.debug("generating %s.html", '.'.join(path))

    # Call all registered page begin hooks
    for hook in state.hooks_pre_page: hook()

    url_base = ''
    breadcrumb = []
    for i in path:
        url_base += i + '.'
        breadcrumb += [(i, url_base + 'html')]

    page = Empty()
    page.summary = extract_summary(state, state.class_docs, path, class_.__doc__)
    page.url = breadcrumb[-1][1]
    page.breadcrumb = breadcrumb
    page.prefix_wbr = '.<wbr />'.join(path + [''])
    page.classes = []
    page.enums = []
    page.classmethods = []
    page.staticmethods = []
    page.dunder_methods = []
    page.methods = []
    page.properties = []
    page.data = []
    page.has_enum_details = False

    # External page content, if provided
    path_str = '.'.join(path)
    if path_str in state.class_docs:
        page.content = render_rst(state, state.class_docs[path_str]['content'])
        state.class_docs[path_str]['used'] = True

    # Index entry for this module, returned together with children at the end
    index_entry = IndexEntry()
    index_entry.kind = 'class'
    index_entry.name = breadcrumb[-1][0]
    index_entry.url = page.url
    index_entry.summary = page.summary

    # List of inner classes to render, these will be done after the current
    # class introspection is done to have some better memory allocation pattern
    classes_to_render = []

    # Get inner classes
    for name, object in inspect.getmembers(class_, lambda o: inspect.isclass(o) and not is_enum(state, o)):
        if name in ['__base__', '__class__']: continue # TODO
        if name.startswith('_'): continue

        subpath = path + [name]
        if not object.__doc__: logging.warning("%s is undocumented", '.'.join(subpath))

        page.classes += [extract_class_doc(state, subpath, object)]
        classes_to_render += [(subpath, object)]

    # Get enums
    for name, object in inspect.getmembers(class_, lambda o: is_enum(state, o)):
        if name.startswith('_'): continue

        subpath = path + [name]
        if not object.__doc__: logging.warning("%s is undocumented", '.'.join(subpath))

        enum_ = extract_enum_doc(state, subpath, object)
        page.enums += [enum_]
        if enum_.has_details: page.has_enum_details = True

    # Get methods
    for name, object in inspect.getmembers(class_, inspect.isroutine):
        # Filter out underscored methods (but not dunder methods)
        if is_internal_function_name(name): continue

        # Filter out dunder methods that don't have their own docs
        if name.startswith('__') and (name, object.__doc__) in _filtered_builtin_functions: continue

        subpath = path + [name]
        if not object.__doc__: logging.warning("%s() is undocumented", '.'.join(subpath))

        for function in extract_function_doc(state, class_, subpath, object):
            if name.startswith('__'):
                page.dunder_methods += [function]
            elif function.is_classmethod:
                page.classmethods += [function]
            elif function.is_staticmethod:
                page.staticmethods += [function]
            else:
                page.methods += [function]

    # Get properties
    for name, object in inspect.getmembers(class_, inspect.isdatadescriptor):
        if (name, object.__doc__) in _filtered_builtin_properties:
            continue
        if name.startswith('_'): continue # TODO: are there any dunder props?

        subpath = path + [name]
        if not object.__doc__: logging.warning("%s is undocumented", '.'.join(subpath))

        page.properties += [extract_property_doc(state, subpath, object)]

    # Get data
    # TODO: unify this query
    for name, object in inspect.getmembers(class_, lambda o: not inspect.ismodule(o) and not inspect.isclass(o) and not inspect.isroutine(o) and not inspect.isframe(o) and not inspect.istraceback(o) and not inspect.iscode(o) and not inspect.isdatadescriptor(o)):
        if name.startswith('_'): continue

        subpath = path + [name]
        page.data += [extract_data_doc(state, class_, subpath, object)]

    # Render the class, free the page data to avoid memory rising indefinitely
    render(state.config, 'class.html', page, env)
    del page

    # Render subclasses
    for subpath, object in classes_to_render:
        index_entry.children += [render_class(state, subpath, object, env)]

    return index_entry

def publish_rst(state: State, source, translator_class=m.htmlsanity.SaneHtmlTranslator):
    pub = docutils.core.Publisher(
        writer=m.htmlsanity.SaneHtmlWriter(),
        source_class=docutils.io.StringInput,
        destination_class=docutils.io.StringOutput)
    pub.set_components('standalone', 'restructuredtext', 'html')
    pub.writer.translator_class = translator_class
    pub.process_programmatic_settings(None, m.htmlsanity.docutils_settings, None)
    # Docutils uses a deprecated U mode for opening files, so instead of
    # monkey-patching docutils.io.FileInput to not do that (like Pelican does),
    # I just read the thing myself.
    # TODO *somehow* need to supply the filename to it for better error
    # reporting, this is too awful
    pub.set_source(source=source)
    pub.publish()

    # External images to pull later
    # TODO: some actual path handling
    for image in pub.document.traverse(docutils.nodes.image):
        state.external_data.add(image['uri'])

    return pub

def render_rst(state: State, source):
    return publish_rst(state, source).writer.parts.get('body').rstrip()

class _SaneInlineHtmlTranslator(m.htmlsanity.SaneHtmlTranslator):
    # Unconditionally force compact paragraphs. This means the inline HTML
    # won't be wrapped in a <p> which is exactly what we want.
    def should_be_compact_paragraph(self, node):
        return True

def render_inline_rst(state: State, source):
    return publish_rst(state, source, _SaneInlineHtmlTranslator).writer.parts.get('body').rstrip()

def render_doc(state: State, filename):
    logging.debug("parsing docs from %s", filename)

    # Page begin hooks are called before this in run(), once for all docs since
    # these functions are not generating any pages

    # Render the file. The directives should take care of everything, so just
    # discard the output afterwards.
    with open(filename, 'r') as f: publish_rst(state, f.read())

def render_page(state: State, path, filename, env):
    logging.debug("generating %s.html", '.'.join(path))

    # Call all registered page begin hooks
    for hook in state.hooks_pre_page: hook()

    # Render the file
    with open(filename, 'r') as f: pub = publish_rst(state, f.read())

    # Extract metadata from the page
    metadata = {}
    for docinfo in pub.document.traverse(docutils.nodes.docinfo):
        for element in docinfo.children:
            if element.tagname == 'field':
                name_elem, body_elem = element.children
                name = name_elem.astext()
                if name in state.config['FORMATTED_METADATA']:
                    # If the metadata are formatted, format them. Use a special
                    # translator that doesn't add <dd> tags around the content,
                    # also explicitly disable the <p> around as we not need it
                    # always.
                    # TODO: uncrapify this a bit
                    visitor = m.htmlsanity._SaneFieldBodyTranslator(pub.document)
                    visitor.compact_field_list = True
                    body_elem.walkabout(visitor)
                    value = visitor.astext()
                else:
                    value = body_elem.astext()
                metadata[name.lower()] = value

    # Breadcrumb, we don't do page hierarchy yet
    assert len(path) == 1
    breadcrumb = [(pub.writer.parts.get('title'), path[0] + '.html')]

    page = Empty()
    page.url = breadcrumb[-1][1]
    page.breadcrumb = breadcrumb
    page.prefix_wbr = path[0]

    # Set page content and add extra metadata from there
    page.content = pub.writer.parts.get('body').rstrip()
    for key, value in metadata.items(): setattr(page, key, value)
    if not hasattr(page, 'summary'): page.summary = ''

    render(state.config, 'page.html', page, env)

    # Index entry for this page, return only if it's not an index
    if path == ['index']: return []
    index_entry = IndexEntry()
    index_entry.kind = 'page'
    index_entry.name = breadcrumb[-1][0]
    index_entry.url = page.url
    index_entry.summary = page.summary
    return [index_entry]

def run(basedir, config, templates):
    # Prepare Jinja environment
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(templates), trim_blocks=True,
        lstrip_blocks=True, enable_async=True)
    # Filter to return file basename or the full URL, if absolute
    def basename_or_url(path):
        if urllib.parse.urlparse(path).netloc: return path
        return os.path.basename(path)
    # Filter to return URL for given symbol or the full URL, if absolute
    def path_to_url(path):
        if urllib.parse.urlparse(path).netloc: return path
        return path + '.html'
    env.filters['basename_or_url'] = basename_or_url
    env.filters['path_to_url'] = path_to_url
    env.filters['urljoin'] = urljoin

    # Populate the INPUT, if not specified, make it absolute
    if config['INPUT'] is None: config['INPUT'] = basedir
    else: config['INPUT'] = os.path.join(basedir, config['INPUT'])

    # Make the output dir absolute
    config['OUTPUT'] = os.path.join(config['INPUT'], config['OUTPUT'])
    if not os.path.exists(config['OUTPUT']): os.makedirs(config['OUTPUT'])

    # Guess MIME type of the favicon
    if config['FAVICON']:
        config['FAVICON'] = (config['FAVICON'], mimetypes.guess_type(config['FAVICON'])[0])

    state = State(config)

    # Set up extra plugin paths. The one for m.css plugins was added above.
    for path in config['PLUGIN_PATHS']:
        if path not in sys.path: sys.path.append(os.path.join(config['INPUT'], path))

    # Import plugins
    for plugin in ['m.htmlsanity'] + config['PLUGINS']:
        module = importlib.import_module(plugin)
        module.register_mcss(
            mcss_settings=config,
            jinja_environment=env,
            module_doc_contents=state.module_docs,
            class_doc_contents=state.class_docs,
            data_doc_contents=state.data_docs,
            hooks_pre_page=state.hooks_pre_page,
            hooks_post_run=state.hooks_post_run)

    # Call all registered page begin hooks for the first time
    for hook in state.hooks_pre_page: hook()

    # First process the doc input files so we have all data for rendering
    # module pages
    for file in config['INPUT_DOCS']:
        render_doc(state, os.path.join(basedir, file))

    for module in config['INPUT_MODULES']:
        if isinstance(module, str):
            module_name = module
            module = importlib.import_module(module)
        else:
            module_name = module.__name__

        state.class_index += [render_module(state, [module_name], module, env)]

    # Warn if there are any unused contents left after processing everything
    unused_module_docs = [key for key, value in state.module_docs.items() if not 'used' in value]
    unused_class_docs = [key for key, value in state.class_docs.items() if not 'used' in value]
    unused_data_docs = [key for key, value in state.data_docs.items() if not 'used' in value]
    if unused_module_docs:
        logging.warning("The following module doc contents were unused: %s", unused_module_docs)
    if unused_class_docs:
        logging.warning("The following class doc contents were unused: %s", unused_class_docs)
    if unused_data_docs:
        logging.warning("The following data doc contents were unused: %s", unused_data_docs)

    for page in config['INPUT_PAGES']:
        state.page_index += render_page(state, [os.path.splitext(os.path.basename(page))[0]], os.path.join(config['INPUT'], page), env)

    # Recurse into the tree and mark every node that has nested modules with
    # has_nestaable_children.
    def mark_nested_modules(list: List[IndexEntry]):
        has_nestable_children = False
        for i in list:
            if i.kind != 'module': continue
            has_nestable_children = True
            i.has_nestable_children = mark_nested_modules(i.children)
        return has_nestable_children
    mark_nested_modules(state.class_index)

    # Create module and class index
    index = Empty()
    index.classes = state.class_index
    index.pages = state.page_index
    for file in ['modules.html', 'classes.html', 'pages.html']:
        template = env.get_template(file)
        rendered = template.render(index=index, FILENAME=file, **config)
        with open(os.path.join(config['OUTPUT'], file), 'wb') as f:
            f.write(rendered.encode('utf-8'))
            # Add back a trailing newline so we don't need to bother with
            # patching test files to include a trailing newline to make Git
            # happy
            # TODO could keep_trailing_newline fix this better?
            f.write(b'\n')

    # Create index.html if it was not provided by the user
    if 'index.rst' not in [os.path.basename(i) for i in config['INPUT_PAGES']]:
        logging.debug("writing index.html for an empty main page")

        page = Empty()
        page.breadcrumb = [(config['PROJECT_TITLE'], 'index.html')]
        page.url = page.breadcrumb[-1][1]
        render(config, 'page.html', page, env)

    # Copy referenced files
    for i in config['STYLESHEETS'] + config['EXTRA_FILES'] + ([config['FAVICON'][0]] if config['FAVICON'] else []) + list(state.external_data) + ([] if config['SEARCH_DISABLED'] else ['search.js']):
        # Skip absolute URLs
        if urllib.parse.urlparse(i).netloc: continue

        # If file is found relative to the conf file, use that
        if os.path.exists(os.path.join(config['INPUT'], i)):
            i = os.path.join(config['INPUT'], i)

        # Otherwise use path relative to script directory
        else:
            i = os.path.join(os.path.dirname(os.path.realpath(__file__)), i)

        logging.debug("copying %s to output", i)
        shutil.copy(i, os.path.join(config['OUTPUT'], os.path.basename(i)))

    # Call all registered finalization hooks for the first time
    for hook in state.hooks_post_run: hook()

if __name__ == '__main__': # pragma: no cover
    parser = argparse.ArgumentParser()
    parser.add_argument('conf', help="configuration file")
    parser.add_argument('--templates', help="template directory", default=default_templates)
    parser.add_argument('--debug', help="verbose debug output", action='store_true')
    args = parser.parse_args()

    # Load configuration from a file, update the defaults with it
    config = copy.deepcopy(default_config)
    name, _ = os.path.splitext(os.path.basename(args.conf))
    module = SourceFileLoader(name, args.conf).load_module()
    if module is not None:
        config.update((k, v) for k, v in inspect.getmembers(module) if k.isupper())

    if args.debug:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    run(os.path.dirname(os.path.abspath(args.conf)), config, os.path.abspath(args.templates))
