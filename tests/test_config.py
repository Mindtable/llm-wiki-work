import os
import tempfile
import unittest
from pathlib import Path

from wiki_tools.config import load_config
from wiki_tools.errors import WikiError


def make_root(config: str, *, agents=("librarian", "wiki-maintainer", "wiki-source-sync")) -> Path:
    root = Path(tempfile.mkdtemp(prefix="wiki-config-")).resolve()
    (root / ".opencode" / "agents").mkdir(parents=True)
    (root / "wiki.toml").write_text(config, encoding="utf-8")
    for name in agents:
        (root / ".opencode" / "agents" / f"{name}.md").write_text(
            "---\ndescription: test profile\nmode: primary\n---\nRead only.\n",
            encoding="utf-8",
        )
    return root


class WikiErrorTests(unittest.TestCase):
    def test_error_exposes_stable_code_and_message(self):
        error = WikiError("configuration_error", "model is not configured")
        self.assertEqual(error.code, "configuration_error")
        self.assertEqual(str(error), "model is not configured")


class ConfigTests(unittest.TestCase):
    def test_load_config_uses_explicit_root_and_resolves_relative_executable(self):
        root = make_root(
            '[ask]\nmodel = "provider/model"\nexecutable = "bin/opencode"\n'
        )
        executable = root / "bin" / "opencode"
        executable.parent.mkdir()
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        executable.chmod(0o755)

        config = load_config(root, purpose="ask")

        self.assertEqual(config.root, root)
        self.assertEqual(config.executable, str(executable))
        self.assertEqual(config.agent, "librarian")
        self.assertEqual(config.model, "provider/model")
        self.assertIn("Read only.", config.profile_prompt)

    def test_nested_provider_model_id_is_valid(self):
        root = make_root(
            '[ask]\nmodel = "openrouter/anthropic/claude-sonnet-4"\n'
        )

        config = load_config(root)

        self.assertEqual(config.model, "openrouter/anthropic/claude-sonnet-4")

    def test_unconfigured_model_is_an_explicit_configuration_error(self):
        root = make_root("[ask]\n")

        with self.assertRaises(WikiError) as raised:
            load_config(root)

        self.assertEqual(raised.exception.code, "configuration_error")
        self.assertIn("model", raised.exception.message.lower())

    def test_maintenance_inherits_limits_but_selects_maintainer(self):
        root = make_root(
            '[ask]\nmodel = "provider/model"\nvariant = "high"\n'
            "timeout_seconds = 37\nmax_steps = 6\nagent = \"librarian\"\n"
        )

        config = load_config(root, purpose="maintenance")

        self.assertEqual(config.agent, "wiki-maintainer")
        self.assertEqual(config.model, "provider/model")
        self.assertEqual(config.variant, "high")
        self.assertEqual(config.timeout_seconds, 37)
        self.assertEqual(config.max_steps, 6)

    def test_confluence_inherits_limits_and_selects_source_sync_profile(self):
        root = make_root(
            '[ask]\nmodel = "provider/model"\nvariant = "high"\n'
            "timeout_seconds = 37\nmax_steps = 6\nagent = \"librarian\"\n"
        )

        config = load_config(root, purpose="confluence")

        self.assertEqual(config.agent, "wiki-source-sync")
        self.assertEqual(config.model, "provider/model")
        self.assertEqual(config.variant, "high")
        self.assertEqual(config.timeout_seconds, 37)
        self.assertEqual(config.max_steps, 6)
        self.assertEqual(config.purpose, "confluence")

    def test_unknown_ask_settings_still_fail(self):
        root = make_root('[ask]\nmodel = "provider/model"\nnew_option = true\n')

        with self.assertRaises(WikiError) as raised:
            load_config(root, purpose="confluence")

        self.assertEqual(raised.exception.code, "configuration_error")
        self.assertIn("new_option", raised.exception.message)

    def test_missing_or_non_primary_agent_fails_before_opencode_fallback(self):
        root = make_root('[ask]\nmodel = "provider/model"\n')
        profile = root / ".opencode" / "agents" / "librarian.md"
        profile.write_text("---\nmode: subagent\n---\n", encoding="utf-8")

        with self.assertRaises(WikiError) as raised:
            load_config(root)

        self.assertEqual(raised.exception.code, "configuration_error")
        self.assertIn("primary", raised.exception.message.lower())

    def test_relative_root_is_rejected(self):
        with self.assertRaises(WikiError) as raised:
            load_config(Path("."))

        self.assertEqual(raised.exception.code, "configuration_error")


if __name__ == "__main__":
    unittest.main()
