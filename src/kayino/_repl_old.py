_variable_kind = Literal["pattern", "text", "map", "token"]
_variable_regex = "|".join(varname for varname in get_args(_variable_kind))
_variable_declaration_regex = re.compile(
    rf"(?P<target>spec)?\s*(?P<kind>{_variable_regex})\s+(?P<name>\w+)\s*=\s*(?P<value>.+)$"
)
_update_target_type = Literal["state", "spec"]
_shorthand_to_field = {
    "text": "texts",
    "map": "mappings",
    "token": "token_set",
    "pattern": "patterns",
}


def insert_variable(
    value: str,
    name: str,
    kind: _variable_kind,
    target: _update_target_type,
    state: ReplState,
) -> ReplState:
    field = _shorthand_to_field[kind]
    if target == "spec":
        old_value = getattr(state.spec, field)
        new_value = old_value | {name: value}
        new_spec = evolve(state.spec, **{field: new_value})
        new_state = evolve(state, spec=new_spec)
    else:
        old_value = getattr(state, field)
        new_value = old_value | {name: value}
        new_state = evolve(state, **{field: new_value})
    return new_state


def _ensure_target(kwargs: dict[str, str]):
    kwargs["target"] = kwargs.get("target") or "state"
    return kwargs


def validate_variable_declaration(
    user_response: str,
    state: ReplState,
) -> tuple[str, ReplState] | None:
    match = _variable_declaration_regex.match(user_response)
    if match:
        kwargs = match.groupdict()
        kwargs = _ensure_target(kwargs)
        if kwargs["target"] == "spec":
            system_response = (
                f"Set {kwargs['kind']} {kwargs['name']} for active spec..."
            )
        else:
            system_response = (
                f"Set {kwargs['kind']} {kwargs['name']} for current state..."
            )
        new_state = insert_variable(**kwargs, state=state)
        return system_response, new_state


_print_regex = re.compile(
    rf"print\s*(?P<target>spec)?\s+(?P<kind>{_variable_regex})s?$"
)


def validate_print_variables(
    user_response: str,
    state: ReplState,
) -> tuple[str, ReplState]:
    if match := _print_regex.match(user_response):
        kwargs = match.groupdict()
        kwargs = _ensure_target(kwargs)
        system_response = print_variables(**kwargs, state=state)
        return system_response, state


def print_variables(
    kind: _variable_kind,
    target: _update_target_type,
    state: ReplState,
) -> str:
    field = _shorthand_to_field[kind]
    if target == "spec":
        variables = getattr(state.spec, field)
    else:
        variables = getattr(state, field)

    return tabulate(variables.items())


_normalize_regex = re.compile(r"norm(?:alize)?\s+(.*)$")
_text_type = Literal["variable", "free_text"]
_variable_prefix = "$"


def validate_normalize(
    user_response: str,
    state: ReplState,
) -> tuple[str, ReplState]:
    if match := _normalize_regex.match(user_response):
        text: str = match.group(1)
        if text.startswith(_variable_prefix):
            text_value = state.texts[text.removeprefix(_variable_prefix)]
        else:
            text_value = text

        norm, status, payload = normalize(
            text=text_value,
            spec=state.spec,
        )
        system_response = f"Norm output: {norm}"
        if status == "illicit_token":
            system_response += f" (illicit tokens: {payload})"
        elif status == "illicit_pattern":
            system_response += " (did not match a known pattern)"
        else:
            system_response += f" (matched patterns: {payload})"
        return system_response, state


_save_cache_regex = re.compile(r"save (?P<name>.*)")


def validate_save_cache(
    user_response: str,
    state: ReplState,
) -> tuple[str, ReplState]:
    if match := _save_cache_regex.match(user_response):
        cache_name = match.groupdict()["name"]
        cache_path = _state_cache_dir / cache_name
        json_str = json.dumps(unstructure(state))
        cache_path.write_text(json_str)
        return f"Saved state to {cache_path}", state


_load_cache_regex = re.compile(r"load (?P<name>.*)")


def validate_load_cache(
    user_response: str,
    state: ReplState,
) -> tuple[str, ReplState]:
    if match := _load_cache_regex.match(user_response):
        cache_name = match.groupdict()["name"]
        cache_path = _state_cache_dir / cache_name
        json_data = json.loads(cache_path.read_bytes())
        new_state = structure(json_data, ReplState)
        return f"Read state from {cache_path}", new_state


def validate_print_tokens(
    user_response: str,
    state: ReplState,
) -> tuple[str, ReplState]:
    if user_response == "tokentest":
        return (
            FormattedText(
                [
                    (TokenStyles["token"], "apri"),
                    (TokenStyles["operator"], "? "),
                    (TokenStyles["unrecognized"], "jicelo"),
                ]
            ),
            state,
        )


def handle_user_response(
    user_response: str,
    state: ReplState,
) -> tuple[str, ReplState]:
    for validate_fn in [
        validate_variable_declaration,
        validate_print_variables,
        validate_normalize,
        validate_save_cache,
        validate_load_cache,
        validate_print_tokens,
    ]:
        if response_tuple := validate_fn(user_response, state):
            return response_tuple
    return "Unrecognized command!", state


def repl():
    session = prompt_toolkit.PromptSession()
    state = ReplState()

    while True:
        try:
            user_response = session.prompt("kyn: ")
        except KeyboardInterrupt:
            continue
        except EOFError:
            break
        else:
            system_response, state = handle_user_response(user_response, state)
            prompt_toolkit.print_formatted_text(system_response)
    print("GoodBye!")


if __name__ == "__main__":
    repl()
