"""
Entrypoint for Kayino REPL.
"""

import prompt_toolkit
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.contrib.regular_languages.completion import GrammarCompleter
from prompt_toolkit.contrib.regular_languages.lexer import GrammarLexer
from prompt_toolkit.lexers import SimpleLexer
from prompt_toolkit.styles import Style

from kayino.repl.commands import repl_turn
from kayino.repl.grammar import create_grammar
from kayino.repl.models import ReplState
from kayino.repl.styles import INPUT_STYLES

def repl():
    session = prompt_toolkit.PromptSession()
    state = ReplState()

    grammar = create_grammar()

    lexer = GrammarLexer(
        grammar,
        lexers={key: SimpleLexer(f"class:{key}") for key in INPUT_STYLES.keys()},
    )

    completer = GrammarCompleter(
        grammar,
        {
            "verb": WordCompleter(["add"]),
        },
    )
    input_style = Style.from_dict(INPUT_STYLES)

    while True:
        try:
            user_response = session.prompt(
                "kyn: ",
                lexer=lexer,
                completer=completer,
                style=input_style,
            )
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
