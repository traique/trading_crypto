"""Model-name validation: OpenAI-compatible gateways serve too many models to keep a list, so any name is accepted."""


def validate_model(provider: str, model: str) -> bool:
    return bool(model)
