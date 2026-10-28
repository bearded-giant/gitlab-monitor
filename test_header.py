from gitlab_monitor.formatting import _header_layout, _key_lines

ROWS = [
    [("r", "refresh"), ("b", "browser"), ("y", "copy url")],
    [("p", "pipelines"), ("k", "commits"), ("g", "goto MR")],
    [("a", "approve"), ("c", "comment"), None],
    [("q", "back")],
]


def test_wide_terminals_get_more_key_columns():
    assert [_header_layout(w)[1] for w in (120, 130, 150, 175, 240, None)] == [2, 3, 4, 5, 5, 3]


def test_key_rows_reflow_to_column_count_and_drop_gaps():
    three = [str(line) for line in _key_lines(ROWS, cols=3)]
    assert len(three) == 3
    assert three[2].split() == ["<a>", "approve", "<c>", "comment", "<q>", "back"]
    five = [str(line) for line in _key_lines(ROWS, cols=5)]
    assert len(five) == 2
    assert five[1].split() == ["<g>", "goto", "MR", "<a>", "approve", "<c>", "comment", "<q>", "back"]
