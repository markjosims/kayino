"""
Entrypoint for Kayino REPL.
"""

import string
import unicodedata
from bisect import bisect_left
from pathlib import Path

import prompt_toolkit
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.contrib.regular_languages.completion import GrammarCompleter
from prompt_toolkit.contrib.regular_languages.lexer import GrammarLexer
from prompt_toolkit.document import Document
from prompt_toolkit.layout.processors import (
    Processor,
    Transformation,
    TransformationInput,
)
from prompt_toolkit.layout.utils import explode_text_fragments
from prompt_toolkit.lexers import SimpleLexer
from prompt_toolkit.styles import Style

from kayino.prebuilt import _point_to_char, _unicode_token, universal_preproc
from kayino.repl.commands import repl_turn
from kayino.repl.grammar import create_grammar
from kayino.repl.models import ReplState
from kayino.repl.styles import INPUT_STYLES

_state_cache_dir = Path(".cache")
_state_cache_dir.mkdir(exist_ok=True)


def _expand_unicode_input(buffer: Buffer) -> None:
    cursor = buffer.cursor_position
    adjustment = 0

    def replace(match):
        nonlocal adjustment
        char = _point_to_char(match)
        if not char.isprintable():
            return match.group()

        if match.start() < cursor:
            adjustment += 1 - (min(cursor, match.end()) - match.start())
        return char

    expanded = _unicode_token.sub(replace, buffer.text)
    if expanded != buffer.text:
        buffer.document = Document(expanded, cursor + adjustment)


class _ShowStandaloneMarks(Processor):
    def apply_transformation(self, ti: TransformationInput) -> Transformation:
        fragments = []
        source_positions = []
        display_positions = []
        after_space_or_punct = True
        for position, fragment in enumerate(explode_text_fragments(ti.fragments)):
            style, char, *metadata = fragment
            is_mark = unicodedata.category(char).startswith("M")
            if after_space_or_punct and is_mark:
                display_positions.append(position + len(source_positions))
                source_positions.append(position)
                fragments.append((f"{style} dim", "x", *metadata))
            fragments.append(fragment)
            if not is_mark:
                after_space_or_punct = char.isspace() or char in string.punctuation

        return Transformation(
            fragments,
            source_to_display=lambda pos: pos + bisect_left(source_positions, pos),
            display_to_source=lambda pos: pos - bisect_left(display_positions, pos),
        )


def repl():
    session = prompt_toolkit.PromptSession(input_processors=[_ShowStandaloneMarks()])
    session.default_buffer.on_text_insert += _expand_unicode_input
    state = ReplState()

    grammar = create_grammar()

    lexer = GrammarLexer(
        grammar,
        lexers={
            member.name: SimpleLexer(f"class:{member.name}") for member in INPUT_STYLES
        },
    )

    completer = GrammarCompleter(
        grammar,
        {
            "verb": WordCompleter(["add"]),
        },
    )
    input_style = Style.from_dict(
        {member.name: member.value for member in INPUT_STYLES}
    )

    while True:
        try:
            user_response = session.prompt(
                "kyn: ",
                lexer=lexer,
                completer=completer,
                style=input_style,
            )
            if user_response:
                user_response = universal_preproc(user_response)
        except KeyboardInterrupt:
            continue
        except EOFError:
            break
        else:
            system_response, state = repl_turn(
                user_response,
                state,
                grammar,
            )
            prompt_toolkit.print_formatted_text(system_response)
    print("GoodBye!")


if __name__ == "__main__":
    repl()
