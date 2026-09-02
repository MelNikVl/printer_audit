from printaudit.security.csrf import csrf_tokens_match, generate_csrf_token


def test_generated_tokens_are_random_and_nonempty():
    a = generate_csrf_token()
    b = generate_csrf_token()
    assert a and b
    assert a != b


def test_matching_tokens_pass():
    token = generate_csrf_token()
    assert csrf_tokens_match(token, token) is True


def test_mismatched_tokens_fail():
    assert csrf_tokens_match(generate_csrf_token(), generate_csrf_token()) is False


def test_empty_or_missing_tokens_fail():
    token = generate_csrf_token()
    assert csrf_tokens_match("", token) is False
    assert csrf_tokens_match(token, "") is False
    assert csrf_tokens_match(None, token) is False
    assert csrf_tokens_match(token, None) is False
    assert csrf_tokens_match(None, None) is False
