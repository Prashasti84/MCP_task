from mcpguard.installer import unwrap, wrap

CONFIG = {
    "mcpServers": {
        "fs": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
               "env": {"A": "1"}},
        "bare": {"command": "my-server"},
        "remote": {"url": "https://example.com/mcp"},
    },
    "other": True,
}


def test_wrap_is_reversible_and_idempotent():
    wrapped, names, skipped = wrap(CONFIG, "/usr/bin/python3")
    assert names == ["fs", "bare"] and skipped == ["remote"]
    fs = wrapped["mcpServers"]["fs"]
    assert fs["command"] == "/usr/bin/python3" and fs["env"] == {"A": "1"}
    assert fs["args"] == ["-m", "mcpguard", "stdio", "--name", "fs", "--", "npx", "-y",
                          "@modelcontextprotocol/server-filesystem", "/tmp"]
    again, names_again, _ = wrap(wrapped, "/usr/bin/python3")
    assert again == wrapped and names_again == []
    restored, names = unwrap(wrapped)
    assert names == ["fs", "bare"]
    assert restored["mcpServers"]["fs"] == CONFIG["mcpServers"]["fs"]
    assert restored["mcpServers"]["bare"] == {"command": "my-server", "args": []}
    assert restored["other"] is True
