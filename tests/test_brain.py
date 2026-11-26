"""Brain-side parsing that guards the body from bad model output."""
from architecture.brain.main import normalize_box
from architecture.brain.mind import _parse_json


def test_box_fraction_passthrough():
    assert normalize_box([0.1, 0.2, 0.3, 0.4]) == [0.1, 0.2, 0.3, 0.4]


def test_box_thousand_grid_is_rescaled():
    # Observed in a live run: the model answered on a 0-1000 grid during re-observation.
    assert normalize_box([765, 600, 800, 715]) == [0.765, 0.6, 0.8, 0.715]


def test_box_garbage_rejected_and_clamped():
    assert normalize_box("box") is None
    assert normalize_box([1, 2]) is None
    assert normalize_box([-0.2, 0.5, 1.2, 0.4]) == [0.0, 0.4, 1.0, 0.5]


def test_json_in_code_fence():
    assert _parse_json('```json\n{"say": "hello"}\n```') == {"say": "hello"}
