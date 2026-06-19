from gitlab_monitor.formatting import next_minor_version


def test_next_minor_version():
    assert next_minor_version("0.25.3") == "0.26.0"
    assert next_minor_version("v1.2.3") == "v1.3.0"
    assert next_minor_version("0.25") == "0.26.0"
    assert next_minor_version("0.9.9") == "0.10.0"
    assert next_minor_version("") == ""
    # unparseable -> verbatim fallback
    assert next_minor_version("nightly") == "nightly"


if __name__ == "__main__":
    test_next_minor_version()
    print("ok")
