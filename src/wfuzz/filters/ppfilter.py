from ..exception import FuzzExceptIncorrectFilter, FuzzExceptBadOptions
from ..helpers.obj_dyn import (
    rgetattr,
    rsetattr,
)
from ..helpers.str_func import value_in_any_list_item

import re
import collections
import operator

# Python 2 and 3: alternative 4
try:
    from urllib.parse import unquote
except ImportError:
    from urllib import unquote

from ..facade import Facade, ERROR_CODE


PYPARSING = True
try:
    from pyparsing import Word, Group, oneOf, Optional, Suppress, ZeroOrMore, Literal, alphas, QuotedString
    from pyparsing import ParseException
except ImportError:
    PYPARSING = False


class FuzzResFilter:
    FUZZ_MARKER_REGEX = re.compile(r"FUZ\d*Z", re.MULTILINE | re.DOTALL)

    def __init__(self, filter_string=None):
        self.filter_string = filter_string
        self.baseline = None

        quoted_str_value = QuotedString('\'', unquoteResults=True, escChar='\\')
        int_values = Word("0123456789").setParseAction(lambda s, l, t: [int(t[0])])
        error_value = Literal("XXX").setParseAction(self.__compute_xxx_value)
        bbb_value = Literal("BBB").setParseAction(self.__compute_bbb_value)
        field_value = Word(alphas + "." + "_" + "-")
        reserverd_words = oneOf("BBB XXX")

        basic_primitives = int_values | quoted_str_value

        operator_names = oneOf("m d e un u r l sw gre gregex unique startswith decode encode unquote replace lower upper").setParseAction(lambda s, l, t: [(l, t[0])])

        fuzz_symbol = (Suppress("FUZ") + Optional(Word("23456789"), 1).setParseAction(lambda s, l, t: [int(t[0])]) + Suppress("Z")).setParseAction(self._compute_fuzz_symbol)
        operator_call = Group(Suppress("|") + operator_names + Suppress("(") + Optional(basic_primitives, None) + Optional(Suppress(",") + basic_primitives, None) + Suppress(")"))

        fuzz_value = (fuzz_symbol + Optional(Suppress("[") + field_value + Suppress("]"), None)).setParseAction(self.__compute_fuzz_value)
        fuzz_value_op = ((fuzz_symbol + Suppress("[") + Optional(field_value)).setParseAction(self.__compute_fuzz_value) + operator_call + Suppress("]")).setParseAction(self.__compute_perl_value)
        fuzz_value_op2 = ((fuzz_symbol + operator_call).setParseAction(self.__compute_perl_value))

        res_value_op = (~reserverd_words + Word("0123456789" + alphas + "." + "_" + "-").setParseAction(self.__compute_res_value) + Optional(operator_call, None)).setParseAction(self.__compute_perl_value)
        basic_primitives_op = (basic_primitives + Optional(operator_call, None)).setParseAction(self.__compute_perl_value)

        fuzz_statement = basic_primitives_op ^ fuzz_value ^ fuzz_value_op ^ fuzz_value_op2 ^ res_value_op

        operator = oneOf("and or")
        not_operator = Optional(oneOf("not"), "notpresent")

        symbol_expr = Group(fuzz_statement + oneOf("= == != < > >= <= =~ !~ ~ := =+ =-") + (bbb_value ^ error_value ^ basic_primitives ^ fuzz_statement)).setParseAction(self.__compute_expr)

        definition = symbol_expr ^ fuzz_statement
        definition_not = not_operator + definition
        definition_expr = definition_not + ZeroOrMore(operator + definition_not)

        nested_definition = Group(Suppress("(") + definition_expr + Suppress(")"))
        nested_definition_not = not_operator + nested_definition

        self.finalformula = (nested_definition_not ^ definition_expr) + ZeroOrMore(operator + (nested_definition_not ^ definition_expr))

        definition_not.setParseAction(self.__compute_not_operator)
        nested_definition_not.setParseAction(self.__compute_not_operator)
        nested_definition.setParseAction(self.__compute_formula)
        self.finalformula.setParseAction(self.__myreduce)

        self.res = None
        self.stack = []
        self._cache = collections.defaultdict(set)

    def set_baseline(self, res):
        self.baseline = res

    def __compute_res_value(self, tokens):
        self.stack.append(tokens[0])

        try:
            return rgetattr(self.res, tokens[0])
        except AttributeError:
            raise FuzzExceptIncorrectFilter("Non-existing introspection field or HTTP parameter \"{}\"!".format(tokens[0]))

    def _compute_fuzz_symbol(self, tokens):
        p_index = tokens[0]

        try:
            return self.res.payload_man.get_payload_content(p_index)
        except IndexError:
            raise FuzzExceptIncorrectFilter("Non existent FUZZ payload! Use a correct index.")

    def __compute_fuzz_value(self, tokens):
        fuzz_val, field = tokens

        if field:
            self.stack.append(field)

        try:
            return rgetattr(fuzz_val, field) if field else fuzz_val
        except IndexError:
            raise FuzzExceptIncorrectFilter("Non existent FUZZ payload! Use a correct index.")
        except AttributeError as e:
            raise FuzzExceptIncorrectFilter("A field expression must be used with a fuzzresult payload not a string. %s" % str(e))

    def __compute_bbb_value(self, tokens):
        element = self.stack.pop() if self.stack else None

        if self.baseline is None:
            raise FuzzExceptBadOptions("FilterQ: specify a baseline value when using BBB")

        if element == 'l' or element == 'lines':
            ret = self.baseline.lines
        elif element == 'c' or element == 'code':
            ret = self.baseline.code
        elif element == 'w' or element == 'words':
            ret = self.baseline.words
        elif element == 'h' or element == 'chars':
            return self.baseline.chars
        elif element == 'index' or element == 'i':
            ret = self.baseline.nres
        else:
            ret = self.baseline.payload_man.get_payload_content(1)

        return ret

    def __compute_perl_value(self, tokens):
        leftvalue, exp = tokens
        # import pdb; pdb.set_trace()

        if exp:
            loc_op, middlevalue, rightvalue = exp
            loc, op = loc_op
        else:
            return leftvalue

        if (op == "un" or op == "unquote") and middlevalue is None and rightvalue is None:
            ret = unquote(leftvalue)
        elif (op == "e" or op == "encode") and middlevalue is not None and rightvalue is None:
            ret = Facade().encoders.get_plugin(middlevalue)().encode(leftvalue)
        elif (op == "d" or op == "decode") and middlevalue is not None and rightvalue is None:
            ret = Facade().encoders.get_plugin(middlevalue)().decode(leftvalue)
        elif op == "r" or op == "replace":
            return leftvalue.replace(middlevalue, rightvalue)
        elif op == "upper":
            return leftvalue.upper()
        elif op == "lower" or op == "l":
            return leftvalue.lower()
        elif op == 'gregex' or op == "gre":
            search_res = None
            try:
                regex = re.compile(middlevalue)
                search_res = regex.search(leftvalue)
            except re.error as e:
                raise FuzzExceptBadOptions("Invalid regex expression used in expression: %s" % str(e))

            if search_res is None:
                return ''
            return search_res.group(1)
        elif op == 'startswith' or op == "sw":
            return leftvalue.strip().startswith(middlevalue)
        elif op == 'unique' or op == "u":
            if leftvalue not in self._cache[loc]:
                self._cache[loc].add(leftvalue)
                return True
            else:
                return False
        else:
            raise FuzzExceptBadOptions("Bad format, expression should be m,d,e,r,s(value,value)")

        return ret

    def __compute_xxx_value(self, tokens):
        return ERROR_CODE

    def __compute_expr(self, tokens):
        leftvalue, exp_operator, rightvalue = tokens[0]

        field_to_set = self.stack.pop() if self.stack else None

        try:
            if exp_operator in ["=", '==']:
                return str(leftvalue) == str(rightvalue)
            elif exp_operator == "<=":
                return leftvalue <= rightvalue
            elif exp_operator == ">=":
                return leftvalue >= rightvalue
            elif exp_operator == "<":
                return leftvalue < rightvalue
            elif exp_operator == ">":
                return leftvalue > rightvalue
            elif exp_operator == "!=":
                return leftvalue != rightvalue
            elif exp_operator == "=~":
                regex = re.compile(rightvalue, re.MULTILINE | re.DOTALL)
                return regex.search(leftvalue) is not None
            elif exp_operator in ["!~", "~"]:
                ret = True

                if isinstance(leftvalue, str):
                    ret = rightvalue.lower() in leftvalue.lower()
                elif isinstance(leftvalue, list):
                    ret = value_in_any_list_item(rightvalue, leftvalue)
                elif isinstance(leftvalue, dict):
                    return len({k: v for (k, v) in leftvalue.items() if rightvalue.lower() in k.lower() or value_in_any_list_item(rightvalue, v)}) > 0
                else:
                    raise FuzzExceptBadOptions("Invalid operand type {}".format(rightvalue))

                return ret if exp_operator == "~" else not ret
            elif exp_operator == ":=":
                rsetattr(self.res, field_to_set, rightvalue, None)
            elif exp_operator == "=+":
                rsetattr(self.res, field_to_set, rightvalue, operator.add)
            elif exp_operator == "=-":
                rsetattr(self.res, field_to_set, rightvalue, lambda x, y: y + x)
        except re.error as e:
            raise FuzzExceptBadOptions("Invalid regex expression used in expression: %s" % str(e))
        except TypeError as e:
            raise FuzzExceptBadOptions("Invalid operand types used in expression: %s" % str(e))
        except ParseException as e:
            raise FuzzExceptBadOptions("Invalid filter: %s" % str(e))

        return True

    def __myreduce(self, elements):
        first = elements[0]
        for i in range(1, len(elements), 2):
            if elements[i] == "and":
                first = (first and elements[i + 1])
            elif elements[i] == "or":
                first = (first or elements[i + 1])

        self.stack = []
        return first

    def __compute_not_operator(self, tokens):
        operator, value = tokens

        if operator == "not":
            return not value

        return value

    def __compute_formula(self, tokens):
        return self.__myreduce(tokens[0])

    def is_active(self):
        return self.filter_string

    def is_visible(self, res, filter_string=None):
        if filter_string is None:
            filter_string = self.filter_string
        self.res = res
        try:
            return self.finalformula.parseString(filter_string, parseAll=True)[0]
        except ParseException as e:
            raise FuzzExceptIncorrectFilter("Incorrect filter expression, check documentation. {}".format(str(e)))
        except AttributeError as e:
            raise FuzzExceptIncorrectFilter("It is only possible to use advanced filters when using a non-string payload. %s" % str(e))

    def get_fuzz_words(self):
        fuzz_words = self.FUZZ_MARKER_REGEX.findall(self.filter_string)

        return fuzz_words


class FuzzResFilterSlice(FuzzResFilter):
    # When using slice we don't have previous payload context but directly a word from the dictionary
    def _compute_fuzz_symbol(self, tokens):
        i = tokens[0]

        if i != 1:
            raise FuzzExceptIncorrectFilter("Non existent FUZZ payload! Use a correct index.")

        return self.res