"""
Entrypoint for Kayino REPL.
"""

from pathlib import Path

import prompt_toolkit
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.contrib.regular_languages.completion import GrammarCompleter
from prompt_toolkit.contrib.regular_languages.lexer import GrammarLexer
from prompt_toolkit.lexers import SimpleLexer
from prompt_toolkit.styles import Style

from kayino.prebuilt import replace_unicode
from kayino.repl.commands import repl_turn
from kayino.repl.grammar import create_grammar
from kayino.repl.models import ReplState
from kayino.repl.styles import INPUT_STYLES

_state_cache_dir = Path(".cache")
_state_cache_dir.mkdir(exist_ok=True)


def repl():
    session = prompt_toolkit.PromptSession()
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
            # for now assuming unicode replacement happens globally
            # for all text inputs
            user_response = replace_unicode(user_response)
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
