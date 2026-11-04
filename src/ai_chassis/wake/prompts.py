"""Lossless launch payloads beside immutable notice snapshots, under their claims."""

from .events import digest


class PromptRefused(ValueError):
    pass


def projection(items, obligations, *, admit=None):
    """Keep notice rows safe; carry prompt bytes outside their lossy formatter."""
    projected, prompts = [], {}
    for token, kind, text in items:
        row = obligations.get(token, {})
        if "launch" in row:
            # Snapshots retain delivered rows until the hook consumes them.
            # Admission survives a publication/ledger interruption and cannot
            # be revoked by a later configuration change.
            if (
                not row["launch"].get("prompt_admitted")
                and row.get("state") != "delivered"
                and not row.get("delivered")
            ):
                validate(text)
                if admit is not None:
                    admit(token)
            name = digest(text)
            prompts[name] = text
            text = f"launch-prompt:{name}"
        projected.append((token, kind, text))
    return projected, prompts


def load_config():
    # The prompt-injection library loads only when a prompt is validated, so a process that
    # never takes the event path never imports it; tests patch this seam for the limit.
    from ai_chassis.prompt_injection.config import load_config as load

    return load()


def validate(text):
    from ai_chassis.prompt_injection.preflight import PayloadTypable

    config = load_config()
    too_long = (
        PayloadTypable._multiline_too_long(text, config["multiline_mode"])
        if "\n" in text
        else len(text) > config["max_typed_chars"]
    )
    if too_long:
        raise PromptRefused("launch-prompt:payload-too-long")
    if not text.strip() or any(
        ord(c) < 32 and c not in "\n\t" or ord(c) == 127  # noqa: PLR2004 - ASCII controls cannot cross the hook boundary
        for c in text
    ):
        raise PromptRefused("launch-prompt:unsupported-text")


def writer(write, prompts):
    def publish(path, text):
        if path.name == "header":
            # The publisher creates a fresh immutable generation. Payloads
            # precede its pointer/readiness commit and share event claims.
            for name, message in prompts.items():
                payload = path.parent / f"{name}.prompt"
                if payload.exists() or payload.is_symlink():
                    raise ValueError("launch-prompt-already-exists")
                write(payload, message + "\n")
                if payload.read_bytes() != (message + "\n").encode():
                    raise ValueError("launch-prompt-write-mismatch")
        return write(path, text)

    return publish
