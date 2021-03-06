import re

from parso.python.token import PythonTokenTypes
from parso.python import tree
from parso.tree import search_ancestor, Leaf

from jedi._compatibility import Parameter
from jedi import debug
from jedi import settings
from jedi.api import classes
from jedi.api import helpers
from jedi.api import keywords
from jedi.api.file_name import complete_file_name
from jedi.inference import imports
from jedi.inference.base_value import ValueSet
from jedi.inference.helpers import infer_call_of_leaf, parse_dotted_names
from jedi.inference.context import get_global_filters
from jedi.inference.value import TreeInstance
from jedi.inference.gradual.conversion import convert_values
from jedi.parser_utils import cut_value_at_position
from jedi.plugins import plugin_manager


def get_signature_param_names(signatures):
    # add named params
    for call_sig in signatures:
        for p in call_sig.params:
            # Allow protected access, because it's a public API.
            if p._name.get_kind() in (Parameter.POSITIONAL_OR_KEYWORD,
                                      Parameter.KEYWORD_ONLY):
                yield p._name


def filter_names(inference_state, completion_names, stack, like_name, fuzzy):
    comp_dct = {}
    if settings.case_insensitive_completion:
        like_name = like_name.lower()
    for name in completion_names:
        string = name.string_name
        if settings.case_insensitive_completion:
            string = string.lower()
        if fuzzy:
            match = helpers.fuzzy_match(string, like_name)
        else:
            match = helpers.start_match(string, like_name)
        if match:
            new = classes.Completion(
                inference_state,
                name,
                stack,
                len(like_name),
                is_fuzzy=fuzzy,
            )
            k = (new.name, new.complete)  # key
            if k in comp_dct and settings.no_completion_duplicates:
                comp_dct[k]._same_name_completions.append(new)
            else:
                comp_dct[k] = new
                yield new


def get_user_context(module_context, position):
    """
    Returns the scope in which the user resides. This includes flows.
    """
    leaf = module_context.tree_node.get_leaf_for_position(position, include_prefixes=True)
    return module_context.create_context(leaf)


def get_flow_scope_node(module_node, position):
    node = module_node.get_leaf_for_position(position, include_prefixes=True)
    while not isinstance(node, (tree.Scope, tree.Flow)):
        node = node.parent

    return node


@plugin_manager.decorate()
def complete_param_names(context, function_name, decorator_nodes):
    # Basically there's no way to do param completion. The plugins are
    # responsible for this.
    return []


class Completion:
    def __init__(self, inference_state, module_context, code_lines, position,
                 signatures_callback, fuzzy=False):
        self._inference_state = inference_state
        self._module_context = module_context
        self._module_node = module_context.tree_node
        self._code_lines = code_lines

        # The first step of completions is to get the name
        self._like_name = helpers.get_on_completion_name(self._module_node, code_lines, position)
        # The actual cursor position is not what we need to calculate
        # everything. We want the start of the name we're on.
        self._original_position = position
        self._position = position[0], position[1] - len(self._like_name)
        self._signatures_callback = signatures_callback

        self._fuzzy = fuzzy

    def complete(self, fuzzy):
        leaf = self._module_node.get_leaf_for_position(self._position, include_prefixes=True)
        string, start_leaf = _extract_string_while_in_string(leaf, self._position)
        if string is not None:
            completions = list(complete_file_name(
                self._inference_state, self._module_context, start_leaf, string,
                self._like_name, self._signatures_callback,
                self._code_lines, self._original_position,
                fuzzy
            ))
            if completions:
                return completions

        completion_names = self._complete_python(leaf)

        completions = filter_names(self._inference_state, completion_names,
                                   self.stack, self._like_name, fuzzy)

        return sorted(completions, key=lambda x: (x.name.startswith('__'),
                                                  x.name.startswith('_'),
                                                  x.name.lower()))

    def _complete_python(self, leaf):
        """
        Analyzes the value that a completion is made in and decides what to
        return.

        Technically this works by generating a parser stack and analysing the
        current stack for possible grammar nodes.

        Possible enhancements:
        - global/nonlocal search global
        - yield from / raise from <- could be only exceptions/generators
        - In args: */**: no completion
        - In params (also lambda): no completion before =
        """

        grammar = self._inference_state.grammar
        self.stack = stack = None

        try:
            self.stack = stack = helpers.get_stack_at_position(
                grammar, self._code_lines, leaf, self._position
            )
        except helpers.OnErrorLeaf as e:
            value = e.error_leaf.value
            if value == '.':
                # After ErrorLeaf's that are dots, we will not do any
                # completions since this probably just confuses the user.
                return []

            # If we don't have a value, just use global completion.
            return self._complete_global_scope()

        allowed_transitions = \
            list(stack._allowed_transition_names_and_token_types())

        if 'if' in allowed_transitions:
            leaf = self._module_node.get_leaf_for_position(self._position, include_prefixes=True)
            previous_leaf = leaf.get_previous_leaf()

            indent = self._position[1]
            if not (leaf.start_pos <= self._position <= leaf.end_pos):
                indent = leaf.start_pos[1]

            if previous_leaf is not None:
                stmt = previous_leaf
                while True:
                    stmt = search_ancestor(
                        stmt, 'if_stmt', 'for_stmt', 'while_stmt', 'try_stmt',
                        'error_node',
                    )
                    if stmt is None:
                        break

                    type_ = stmt.type
                    if type_ == 'error_node':
                        first = stmt.children[0]
                        if isinstance(first, Leaf):
                            type_ = first.value + '_stmt'
                    # Compare indents
                    if stmt.start_pos[1] == indent:
                        if type_ == 'if_stmt':
                            allowed_transitions += ['elif', 'else']
                        elif type_ == 'try_stmt':
                            allowed_transitions += ['except', 'finally', 'else']
                        elif type_ == 'for_stmt':
                            allowed_transitions.append('else')

        completion_names = []
        current_line = self._code_lines[self._position[0] - 1][:self._position[1]]
        if not current_line or current_line[-1] in ' \t.;' \
                and current_line[-3:] != '...':
            completion_names += self._complete_keywords(allowed_transitions)

        if any(t in allowed_transitions for t in (PythonTokenTypes.NAME,
                                                  PythonTokenTypes.INDENT)):
            # This means that we actually have to do type inference.

            nonterminals = [stack_node.nonterminal for stack_node in stack]

            nodes = []
            for stack_node in stack:
                if stack_node.dfa.from_rule == 'small_stmt':
                    nodes = []
                else:
                    nodes += stack_node.nodes

            if nodes and nodes[-1] in ('as', 'def', 'class'):
                # No completions for ``with x as foo`` and ``import x as foo``.
                # Also true for defining names as a class or function.
                return list(self._complete_inherited(is_function=True))
            elif "import_stmt" in nonterminals:
                level, names = parse_dotted_names(nodes, "import_from" in nonterminals)

                only_modules = not ("import_from" in nonterminals and 'import' in nodes)
                completion_names += self._get_importer_names(
                    names,
                    level,
                    only_modules=only_modules,
                )
            elif nonterminals[-1] in ('trailer', 'dotted_name') and nodes[-1] == '.':
                dot = self._module_node.get_leaf_for_position(self._position)
                completion_names += self._complete_trailer(dot.get_previous_leaf())
            elif self._is_parameter_completion():
                completion_names += self._complete_params(leaf)
            else:
                completion_names += self._complete_global_scope()
                completion_names += self._complete_inherited(is_function=False)

            # Apparently this looks like it's good enough to filter most cases
            # so that signature completions don't randomly appear.
            # To understand why this works, three things are important:
            # 1. trailer with a `,` in it is either a subscript or an arglist.
            # 2. If there's no `,`, it's at the start and only signatures start
            #    with `(`. Other trailers could start with `.` or `[`.
            # 3. Decorators are very primitive and have an optional `(` with
            #    optional arglist in them.
            if nodes[-1] in ['(', ','] and nonterminals[-1] in ('trailer', 'arglist', 'decorator'):
                signatures = self._signatures_callback(*self._position)
                completion_names += get_signature_param_names(signatures)

        return completion_names

    def _is_parameter_completion(self):
        tos = self.stack[-1]
        if tos.nonterminal == 'lambdef' and len(tos.nodes) == 1:
            # We are at the position `lambda `, where basically the next node
            # is a param.
            return True
        if tos.nonterminal in 'parameters':
            # Basically we are at the position `foo(`, there's nothing there
            # yet, so we have no `typedargslist`.
            return True
        # var args is for lambdas and typed args for normal functions
        return tos.nonterminal in ('typedargslist', 'varargslist') and tos.nodes[-1] == ','

    def _complete_params(self, leaf):
        stack_node = self.stack[-2]
        if stack_node.nonterminal == 'parameters':
            stack_node = self.stack[-3]
        if stack_node.nonterminal == 'funcdef':
            context = get_user_context(self._module_context, self._position)
            node = search_ancestor(leaf, 'error_node', 'funcdef')
            if node.type == 'error_node':
                n = node.children[0]
                if n.type == 'decorators':
                    decorators = n.children
                elif n.type == 'decorator':
                    decorators = [n]
                else:
                    decorators = []
            else:
                decorators = node.get_decorators()
            function_name = stack_node.nodes[1]

            return complete_param_names(context, function_name.value, decorators)
        return []

    def _complete_keywords(self, allowed_transitions):
        for k in allowed_transitions:
            if isinstance(k, str) and k.isalpha():
                yield keywords.KeywordName(self._inference_state, k)

    def _complete_global_scope(self):
        context = get_user_context(self._module_context, self._position)
        debug.dbg('global completion scope: %s', context)
        flow_scope_node = get_flow_scope_node(self._module_node, self._position)
        filters = get_global_filters(
            context,
            self._position,
            flow_scope_node
        )
        completion_names = []
        for filter in filters:
            completion_names += filter.values()
        return completion_names

    def _complete_trailer(self, previous_leaf):
        inferred_context = self._module_context.create_context(previous_leaf)
        values = infer_call_of_leaf(inferred_context, previous_leaf)
        debug.dbg('trailer completion values: %s', values, color='MAGENTA')
        return self._complete_trailer_for_values(values)

    def _complete_trailer_for_values(self, values):
        user_value = get_user_context(self._module_context, self._position)
        completion_names = []
        for value in values:
            for filter in value.get_filters(origin_scope=user_value.tree_node):
                completion_names += filter.values()

            if not value.is_stub() and isinstance(value, TreeInstance):
                completion_names += self._complete_getattr(value)

        python_values = convert_values(values)
        for c in python_values:
            if c not in values:
                for filter in c.get_filters(origin_scope=user_value.tree_node):
                    completion_names += filter.values()
        return completion_names

    def _complete_getattr(self, instance):
        """
        A heuristic to make completion for proxy objects work. This is not
        intended to work in all cases. It works exactly in this case:

            def __getattr__(self, name):
                ...
                return getattr(any_object, name)

        It is important that the return contains getattr directly, otherwise it
        won't work anymore. It's really just a stupid heuristic. It will not
        work if you write e.g. `return (getatr(o, name))`, because of the
        additional parentheses. It will also not work if you move the getattr
        to some other place that is not the return statement itself.

        It is intentional that it doesn't work in all cases. Generally it's
        really hard to do even this case (as you can see below). Most people
        will write it like this anyway and the other ones, well they are just
        out of luck I guess :) ~dave.
        """
        names = (instance.get_function_slot_names(u'__getattr__')
                 or instance.get_function_slot_names(u'__getattribute__'))
        functions = ValueSet.from_sets(
            name.infer()
            for name in names
        )
        for func in functions:
            tree_node = func.tree_node
            for return_stmt in tree_node.iter_return_stmts():
                # Basically until the next comment we just try to find out if a
                # return statement looks exactly like `return getattr(x, name)`.
                if return_stmt.type != 'return_stmt':
                    continue
                atom_expr = return_stmt.children[1]
                if atom_expr.type != 'atom_expr':
                    continue
                atom = atom_expr.children[0]
                trailer = atom_expr.children[1]
                if len(atom_expr.children) != 2 or atom.type != 'name' \
                        or atom.value != 'getattr':
                    continue
                arglist = trailer.children[1]
                if arglist.type != 'arglist' or len(arglist.children) < 3:
                    continue
                context = func.as_context()
                object_node = arglist.children[0]

                # Make sure it's a param: foo in __getattr__(self, foo)
                name_node = arglist.children[2]
                name_list = context.goto(name_node, name_node.start_pos)
                if not any(n.api_type == 'param' for n in name_list):
                    continue

                # Now that we know that these are most probably completion
                # objects, we just infer the object and return them as
                # completions.
                objects = context.infer_node(object_node)
                return self._complete_trailer_for_values(objects)
        return []

    def _get_importer_names(self, names, level=0, only_modules=True):
        names = [n.value for n in names]
        i = imports.Importer(self._inference_state, names, self._module_context, level)
        return i.completion_names(self._inference_state, only_modules=only_modules)

    def _complete_inherited(self, is_function=True):
        """
        Autocomplete inherited methods when overriding in child class.
        """
        leaf = self._module_node.get_leaf_for_position(self._position, include_prefixes=True)
        cls = tree.search_ancestor(leaf, 'classdef')
        if cls is None:
            return

        # Complete the methods that are defined in the super classes.
        class_value = self._module_context.create_value(cls)

        if cls.start_pos[1] >= leaf.start_pos[1]:
            return

        filters = class_value.get_filters(is_instance=True)
        # The first dict is the dictionary of class itself.
        next(filters)
        for filter in filters:
            for name in filter.values():
                # TODO we should probably check here for properties
                if (name.api_type == 'function') == is_function:
                    yield name


def _extract_string_while_in_string(leaf, position):
    if position < leaf.start_pos:
        return None, None

    if leaf.type == 'string':
        match = re.match(r'^\w*(\'{3}|"{3}|\'|")', leaf.value)
        quote = match.group(1)
        if leaf.line == position[0] and position[1] < leaf.column + match.end():
            return None, None
        if leaf.end_pos[0] == position[0] and position[1] > leaf.end_pos[1] - len(quote):
            return None, None
        return cut_value_at_position(leaf, position)[match.end():], leaf

    leaves = []
    while leaf is not None and leaf.line == position[0]:
        if leaf.type == 'error_leaf' and ('"' in leaf.value or "'" in leaf.value):
            return ''.join(l.get_code() for l in leaves), leaf
        leaves.insert(0, leaf)
        leaf = leaf.get_previous_leaf()
    return None, None
