import importlib
import inspect
import io
import re
import sys
import token
import tokenize
from enum import IntEnum

if sys.version_info < (3, 8):
    token.COLONEQUAL = 0xFF
    token.tok_name[0xFF] = "COLONEQUAL"
    tokenize.EXACT_TOKEN_TYPES[":="] = token.COLONEQUAL

if sys.version_info < (3, 7):
    token.NL = 0xFF - 1
    token.tok_name[0xFF - 1] = "NL"

if not hasattr(token, "EXACT_TOKEN_TYPES"):
    token.EXACT_TOKEN_TYPES = tokenize.EXACT_TOKEN_TYPES

token.EXACT_TOKEN_NAMES = dict(
    zip(token.EXACT_TOKEN_TYPES.values(), token.EXACT_TOKEN_TYPES.keys())
)


class Slice:
    def __init__(self, *args):
        self.s = slice(*args)

    def increase(self, amount):
        start = self.s.start + amount
        stop = self.s.stop + amount
        self.s = slice(start, stop)


class TextStreamTokenPosition:
    def __init__(self, start, end):
        self.start = start
        self.end = end


class Priority(IntEnum):
    FIRST = 0
    AVG = 1
    LAST = 2
    CANCEL_PENDING = 3

    @staticmethod
    def get(func):
        if hasattr(func, "priority"):
            return func.priority
        else:
            return Priority.AVG

    def filter(self, patterns):
        new_patterns = patterns
        for key, func in patterns:
            if self.get(func) < self:
                new_patterns.remove((key, func))
        return new_patterns

    def __call__(self, func):
        func.priority = self
        return func


class PatternError(Exception):
    pass


class Transposer(Exception):
    pass


class NoLineTransposer(Transposer):
    pass


def _clear_name(name):
    if not name.isalpha():
        index = -1
        name_buffer = ""
        current = name[index]
        while current.isalpha():
            name_buffer += current
            index -= 1
            current = name[index]
        index += 1  # pretty hacky, fix it ASAP
        return name[:index], name[index:]
    else:
        return "", name


def _clear_name_by_prefix(name):
    index = 0
    for char in name:
        if not (char.isalpha() or char in "()"):
            index += 1
        else:
            break

    return name[:index], name[index:]


def get_type_from_name(name):
    prefix, name = _clear_name(name)
    if name in token.EXACT_TOKEN_NAMES:
        name = token.EXACT_TOKEN_NAMES[name]
    elif hasattr(token, name):
        name = getattr(token, name)
    else:
        raise ValueError("Invalid token name, {name}.")
    return f"{prefix}{name}"


def pattern(*pattern_tokens):
    def wrapper(func):
        pattern_template_buffer = ""
        for index, pattern_part in enumerate(pattern_tokens):
            prefix, pattern_part = _clear_name_by_prefix(pattern_part)
            add_parenthesis = not (
                pattern_part.startswith("(") and pattern_part.endswith(")")
            )
            if index == 0 or not add_parenthesis:
                template = fr"{pattern_part}"
            elif add_parenthesis:
                template = fr"\s{pattern_part}"

            if add_parenthesis:
                template = fr"({template}){prefix}"
            else:
                template = fr"\s{template}{prefix}"

            pattern_template_buffer += template
        pattern_template = re.compile(pattern_template_buffer, re.I)

        if hasattr(func, "patterns"):
            func.patterns.append(pattern_template)
        else:
            func.patterns = [pattern_template]
        return func

    return wrapper


class TokenTransformer:
    def _next_token_slot(self):
        index = max(token.tok_name.keys(), default=0)
        return index + 1

    def _register_token(self, token_string, token_name):
        token_index = self._next_token_slot()
        setattr(token, token_name, token_index)
        token.tok_name[token_index] = token_name
        token.EXACT_TOKEN_TYPES[token_string] = token_index

    def _register_tokens(self):
        for name, member in inspect.getmembers(self):
            if name.startswith("register_"):
                token_name = name.replace("register_", "", 1).upper()
                token_string = member()
                self._register_token(token_string, token_name)

        importlib.reload(tokenize)

    def _pattern_search(self):
        patterns = {}
        for name, member in inspect.getmembers(self):
            if hasattr(member, "patterns"):
                for pattern in member.patterns:
                    patterns[pattern] = member
        return patterns

    def _get_type(self, stream_token):
        if stream_token.string in token.EXACT_TOKEN_TYPES:
            type = token.EXACT_TOKEN_TYPES[stream_token.string]
        else:
            type = stream_token.type
        return type

    def _slice_replace(self, visitor, pattern_slices, stream_tokens):
        offset = 0
        for pattern_slice in pattern_slices:
            pattern_slice.increase(offset)
            matching_tokens = stream_tokens[pattern_slice.s]
            try:
                tokens = visitor(*matching_tokens) or matching_tokens
                tokens, matching_tokens, stream_tokens = self.set_tokens(
                    tokens, pattern_slice.s, matching_tokens, stream_tokens
                )
            except NoLineTransposer:
                tokens = []
                stream_tokens = self.shift_after(
                    pattern_slice.s.stop, stream_tokens, y_offset=-1
                )

            offset += len(tokens) - len(matching_tokens)
            stream_tokens[pattern_slice.s] = tokens

        return stream_tokens

    def _pattern_transformer_regex(self, patterns, stream_tokens):
        def token_to_text(stream_tokens):
            return " ".join(
                token.tok_name[self._get_type(stream_token)]
                for stream_token in stream_tokens
            )

        def finditer_overlapping(pattern, text):
            for counter in range(len(text)):
                match = pattern.match(text, counter)
                if match:
                    yield match

        def text_stream_searcher(start, end):
            matchings = []
            for (
                stream_tokens_position,
                index,
            ) in stream_tokens_positions.items():
                if (
                    stream_tokens_position.start >= start
                    and stream_tokens_position.end <= end
                ):
                    matchings.append(index)
            try:
                return (
                    matchings[0],
                    matchings[-1] + 1,
                )  # hacky thing that make this work, please fix it ASAP
            except IndexError as exc:
                raise PatternError from exc

        def stream_tokens_reindex():
            stream_tokens_positions.clear()
            offset = 0
            for index, stream_token_text in enumerate(
                stream_tokens_text.split()
            ):
                stream_token_text_length = len(stream_token_text)
                stream_tokens_positions[
                    TextStreamTokenPosition(
                        offset, offset + stream_token_text_length
                    )
                ] = index
                offset += stream_token_text_length + 1  # filler (\s)

        stream_tokens_text = token_to_text(stream_tokens)
        stream_tokens_positions = {}
        stream_tokens_reindex()
        patterns = sorted(
            patterns.items(), key=lambda kv: Priority.get(kv[1]),
        )

        if any(
            k
            for k, v in patterns
            if Priority.get(v) is Priority.CANCEL_PENDING
        ):
            patterns = Priority.CANCEL_PENDING.filter(patterns)

        for pattern, visitor in patterns:
            slices = []
            for match in finditer_overlapping(pattern, stream_tokens_text):
                start_index, end_index = text_stream_searcher(*match.span())
                slices.append(Slice(start_index, end_index))

            stream_tokens = self._slice_replace(visitor, slices, stream_tokens)
            stream_tokens_text = token_to_text(stream_tokens)
            stream_tokens_reindex()

        return stream_tokens

    def _pattern_transformer(self, patterns, stream_tokens):
        for pattern, visitor in patterns.items():
            start_indexes, end_indexes = [], []
            pattern_state = 0

            for token_index, stream_token in enumerate(stream_tokens):
                if pattern_state == len(pattern):
                    pattern_state = 0
                    end_indexes.append(token_index)
                if self._get_type(stream_token) == int(pattern[pattern_state]):
                    if pattern_state == 0:
                        start_indexes.append(token_index)
                    pattern_state += 1
                elif pattern_state != 0:
                    pattern_state = 0
                    start_indexes.pop()
            else:
                if pattern_state != 0:
                    start_indexes.pop()

            slices = [
                Slice(start, end)
                for start, end in zip(start_indexes, end_indexes)
            ]

            stream_tokens = self._slice_replace(visitor, slices, stream_tokens)

        return stream_tokens

    def transform(self, source):
        self._register_tokens()
        patterns = self._pattern_search()

        readline = io.StringIO(source).readline
        stream_tokens = tuple(tokenize.generate_tokens(readline))
        stream_tokens_buffer = []

        for stream_token in stream_tokens:
            name = tokenize.tok_name[self._get_type(stream_token)]
            visitor = getattr(self, f"visit_{name.lower()}", self.dummy)
            stream_tokens_buffer.append(visitor(stream_token) or stream_token)

        stream_tokens_buffer = self._pattern_transformer_regex(
            patterns, stream_tokens_buffer
        )
        stream_tokens = stream_tokens_buffer.copy()
        source = tokenize.untokenize(stream_tokens)
        return source

    def set_tokens(self, new_tokens, pattern, matching_tokens, all_tokens):
        new_start, new_end = new_tokens[0], new_tokens[-1]
        original_start, original_end = matching_tokens[0], matching_tokens[-1]

        if (new_start.start[0] != new_end.end[0]) or (
            original_start.start[0] != original_end.end[0]
        ):
            # check if all the start tokens are in the sameline with their end token
            return new_tokens, matching_tokens, all_tokens

        start_difference = (
            original_start.start[0] - new_start.start[0],
            original_start.start[1] - new_start.start[1],
        )
        new_tokens_buffer = []
        for token in new_tokens:
            for page, difference in enumerate(start_difference):
                token = self.increase(token, amount=difference, page=page)
            new_tokens_buffer.append(token)

        new_token_diff = (
            new_tokens_buffer[-1].end[1] - all_tokens[pattern.stop].start[1]
        )

        all_tokens_buffer = all_tokens[: pattern.stop]
        for token in all_tokens[pattern.stop :]:
            if token.start[0] == new_tokens_buffer[-1].end[0]:
                token = self.increase(token, amount=new_token_diff, page=1)
            all_tokens_buffer.append(token)

        return_value = new_tokens_buffer, matching_tokens, all_tokens_buffer
        return return_value

    def shift_all(self, tokens, x_offset=0, y_offset=0):
        new_tokens = []
        for token in tokens:
            new_token = self.increase(token, amount=y_offset, page=0)
            new_token = self.increase(new_token, amount=x_offset, page=1)
            new_tokens.append(new_token)
        return new_tokens

    def shift_after(self, point, tokens, x_offset=0, y_offset=0):
        prev_tokens = tokens[:point]
        next_tokens = self.shift_all(
            tokens[point:], x_offset=x_offset, y_offset=y_offset
        )
        return prev_tokens + next_tokens

    def directional_length(self, tokens):
        return tokens[-1].end[1] - tokens[0].start[1]

    def increase(self, token, amount=1, page=0):
        # page 0 => line number
        # page 1 => column offset

        start, end = list(token.start), list(token.end)

        start[page] += amount
        end[page] += amount

        return token._replace(start=tuple(start), end=tuple(end))

    def is_valid_position(self, token):
        return all(pos >= 0 for pos in (token.start + token.end))

    def quick_tokenize(self, source):
        return list(tokenize.generate_tokens(io.StringIO(source).readline))[
            :-2
        ]

    def dummy(self, token):
        # Implement dummy on subclasses for logging purposes or getting all tokens
        return None
