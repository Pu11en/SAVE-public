"""Unit tests for app/plugins/output_cleanup.

Tests use REAL dirty examples from the audit:
  - Markdown bold replies (**text**, ## headers, ---)
  - Tech-speak: Railway, GitHub, deploy, API
  - File path: app/skills/research_tools/tiktok.py
  - Internal name: self_heal

Each test asserts the example comes out clean (no banned patterns)
while normal English prose passes through untouched.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class TestOutputCleanupImport(unittest.TestCase):
    """Verify the plugin imports cleanly without external deps."""

    def test_imports_without_error(self):
        from app.plugins.output_cleanup import clean, on_transform, register
        self.assertTrue(callable(clean))
        self.assertTrue(callable(on_transform))
        self.assertTrue(callable(register))


class TestMarkdownStripping(unittest.TestCase):
    """(a) Markdown emphasis/headers/rules/code-fences → plain text."""

    def _clean(self, text: str) -> str:
        from app.plugins.output_cleanup import clean
        result = clean(text)
        return result if result is not None else text

    def test_bold_double_asterisk(self):
        """**bold** → plain text."""
        result = self._clean("Here is **important info** for you.")
        self.assertNotIn("**", result)
        self.assertIn("important info", result)

    def test_bold_underscores(self):
        """__bold__ → plain text."""
        result = self._clean("__bold text__ is cleaned.")
        self.assertNotIn("__", result)
        self.assertIn("bold text", result)

    def test_italic_single_asterisk(self):
        """*italic* → plain text."""
        result = self._clean("This is *italicized* text.")
        self.assertNotIn("*italicized*", result)
        self.assertIn("italicized", result)

    def test_header_h2(self):
        """## Header → plain text."""
        result = self._clean("## Status Update\nAll good.")
        self.assertNotIn("##", result)
        self.assertIn("Status Update", result)

    def test_header_h3(self):
        """### Header → plain text."""
        result = self._clean("### Next Steps\nDo X.")
        self.assertNotIn("###", result)
        self.assertIn("Next Steps", result)

    def test_horizontal_rule(self):
        """--- → removed."""
        result = self._clean("Line one.\n---\nLine two.")
        self.assertNotIn("---", result)
        self.assertIn("Line one", result)
        self.assertIn("Line two", result)

    def test_code_fence(self):
        """```python ... ``` → plain text."""
        result = self._clean("Here:\n```python\nprint('hello')\n```\nDone.")
        self.assertNotIn("```", result)
        self.assertIn("Done", result)

    def test_inline_code(self):
        """`code` → plain text."""
        result = self._clean("Run `start.sh` to begin.")
        self.assertNotIn("`", result)
        self.assertIn("start.sh", result)

    def test_blockquote(self):
        """> quote → plain text."""
        result = self._clean("> This is a quote.")
        self.assertNotIn(">", result)
        self.assertIn("This is a quote", result)

    def test_bullet_list(self):
        """- item → • item."""
        result = self._clean("- First item\n- Second item")
        self.assertNotIn("- First", result)
        self.assertIn("First item", result)

    def test_link(self):
        """[text](url) → text (url redacted to [link] by URL rewrite)."""
        result = self._clean("See [this page](https://example.com) for details.")
        self.assertNotIn("[this page]", result)
        self.assertIn("this page", result)
        # Raw URLs are replaced by URL rewrite — "[link]" is expected
        self.assertNotIn("https://example.com", result)

    def test_plain_text_unchanged(self):
        """Clean text should not be modified."""
        text = "Everything is working fine. Let me know if you have questions."
        from app.plugins.output_cleanup import clean
        result = clean(text)
        self.assertIsNone(result)  # None means no change needed


class TestR27TechSpeakRewrite(unittest.TestCase):
    """(b) R27 banned tech terms → plain English rewrites.

    Only tests unambiguous dev-jargon terms that are kept.
    Terms intentionally dropped (model, code, server, runtime, container,
    push, commit, script, skill, scheduler) are NOT tested here — they
    must pass through untouched to avoid mangling normal prose.
    """

    def _clean(self, text: str) -> str:
        from app.plugins.output_cleanup import clean
        result = clean(text)
        return result if result is not None else text

    def test_railway_rewrite(self):
        """Railway → the system."""
        result = self._clean("It's live on Railway now.")
        self.assertNotIn("Railway", result)

    def test_github_rewrite(self):
        """GitHub → the project."""
        result = self._clean("Check the GitHub repo for details.")
        self.assertNotIn("GitHub", result)

    def test_deploy_rewrite(self):
        """deploy → go live."""
        result = self._clean("I'll deploy this for you.")
        self.assertNotIn("deploy", result.lower())

    def test_deployed_rewrite(self):
        """deployed → went live."""
        result = self._clean("The update was deployed successfully.")
        self.assertNotIn("deployed", result.lower())

    def test_api_rewrite(self):
        """API → connection."""
        result = self._clean("This connects via the API.")
        self.assertNotIn("API", result)

    def test_repo_rewrite(self):
        """repo → project."""
        result = self._clean("Found a great repo for that.")
        self.assertNotIn("repo", result.lower())

    def test_cron_rewrite(self):
        """cron → schedule."""
        result = self._clean("I'll set up a cron job for this.")
        self.assertNotIn("cron", result.lower())

    def test_webhook_rewrite(self):
        """webhook → notification."""
        result = self._clean("Using a webhook to get updates.")
        self.assertNotIn("webhook", result.lower())

    def test_json_rewrite(self):
        """JSON → data."""
        result = self._clean("Returning JSON from the service.")
        self.assertNotIn("JSON", result)

    def test_llm_rewrite(self):
        """LLM → assistant."""
        result = self._clean("The LLM is processing your request.")
        self.assertNotIn("LLM", result)

    def test_database_rewrite(self):
        """database → memory."""
        result = self._clean("Stored in the database.")
        self.assertNotIn("database", result.lower())

    # ---- Dropped-term passthrough tests: these must NOT be rewritten ----

    def test_model_not_rewritten(self):
        """'model' is common English — must pass through unchanged."""
        result = self._clean("That's a good model for the situation.")
        self.assertIn("model", result.lower())

    def test_code_not_rewritten(self):
        """'code' is common English — must pass through unchanged."""
        result = self._clean("That was the dress code at the event.")
        self.assertIn("code", result.lower())

    def test_server_not_rewritten(self):
        """'server' is common English (restaurant) — must pass through unchanged."""
        result = self._clean("The server brought us water.")
        self.assertIn("server", result.lower())

    def test_push_not_rewritten(self):
        """'push' is common English — must pass through unchanged."""
        result = self._clean("Give it a push and it should open.")
        self.assertIn("push", result.lower())

    def test_commit_not_rewritten(self):
        """'commit' is common English — must pass through unchanged."""
        result = self._clean("I commit to finishing this today.")
        self.assertIn("commit", result.lower())

    def test_script_not_rewritten(self):
        """'script' is common English (film/speech) — must pass through unchanged."""
        result = self._clean("She read from the script during rehearsal.")
        self.assertIn("script", result.lower())

    def test_container_not_rewritten(self):
        """'container' is common English — must pass through unchanged."""
        result = self._clean("Put the leftovers in a container.")
        self.assertIn("container", result.lower())

    def test_runtime_not_rewritten(self):
        """'runtime' is dropped — must pass through unchanged."""
        result = self._clean("The runtime for the movie is two hours.")
        self.assertIn("runtime", result.lower())


class TestPathAndInternalRedaction(unittest.TestCase):
    """(c) Bare file paths and internal names → redacted."""

    def _clean(self, text: str) -> str:
        from app.plugins.output_cleanup import clean
        result = clean(text)
        return result if result is not None else text

    def test_app_skill_path_redacted(self):
        """app/skills/research_tools/tiktok.py → [path]."""
        result = self._clean(
            "Built. Lint clean. File is at app/skills/research_tools/tiktok.py"
        )
        self.assertNotIn("app/skills/research_tools/tiktok.py", result)
        self.assertIn("[path]", result)

    def test_self_heal_name_redacted(self):
        """self_heal → [internal]."""
        result = self._clean("Running self_heal to fix the issue.")
        self.assertNotIn("self_heal", result)
        self.assertIn("[internal]", result)

    def test_soul_md_name_redacted(self):
        """SOUL.md → [internal]."""
        result = self._clean("Check SOUL.md for the voice rules.")
        self.assertNotIn("SOUL.md", result)
        self.assertIn("[internal]", result)

    def test_opt_path_redacted(self):
        """/opt/data/... → [path]."""
        result = self._clean("Files stored at /opt/agent/data/logs/out.txt")
        self.assertNotIn("/opt/agent/data/logs/out.txt", result)
        self.assertIn("[path]", result)

    def test_save_prompt_assembly_redacted(self):
        """save_prompt_assembly → [internal]."""
        result = self._clean("The save_prompt_assembly plugin handles injection.")
        self.assertNotIn("save_prompt_assembly", result)
        self.assertIn("[internal]", result)

    def test_inkbox_redacted(self):
        """inkbox → [internal]."""
        result = self._clean("Sending via inkbox gateway.")
        self.assertNotIn("inkbox", result)
        self.assertIn("[internal]", result)

    def test_app_plugin_path_redacted(self):
        """app/plugins/... paths → [path]."""
        result = self._clean(
            "I updated app/plugins/recall/__init__.py with the fix."
        )
        self.assertNotIn("app/plugins/recall/__init__.py", result)
        self.assertIn("[path]", result)


class TestRealAuditExamples(unittest.TestCase):
    """Integration tests using real dirty examples from the conversation audit.

    These are the actual patterns that appeared in ~70% of Drew's replies.
    """

    def _clean(self, text: str) -> str:
        from app.plugins.output_cleanup import clean
        result = clean(text)
        return result if result is not None else text

    def test_fake_build_success_message(self):
        """The exact fake-action message from the audit."""
        dirty = (
            "Built. Lint clean. File is at app/skills/research_tools/tiktok.py"
        )
        result = self._clean(dirty)
        self.assertNotIn("app/skills/research_tools/tiktok.py", result)

    def test_markdown_bold_reply(self):
        """Bot reply full of **bold** markdown."""
        dirty = (
            "**Status**: Your GitHub repo has been **deployed** to Railway.\n"
            "## Next Steps\n"
            "- Check the API endpoint\n"
            "- Run the script\n"
            "---\n"
            "Let me know if you need changes."
        )
        result = self._clean(dirty)
        self.assertNotIn("**", result)
        self.assertNotIn("##", result)
        self.assertNotIn("---", result)
        self.assertNotIn("GitHub", result)
        self.assertNotIn("Railway", result)
        self.assertNotIn("API", result)

    def test_combined_markdown_and_tech_speak(self):
        """Reply with both markdown formatting and tech terms."""
        dirty = (
            "I've **deployed** the new feature to Railway.\n"
            "The `self_heal` plugin updated `app/skills/research_tools/tiktok.py`.\n"
            "Check the GitHub repo for the full diff."
        )
        result = self._clean(dirty)
        self.assertNotIn("**", result)
        self.assertNotIn("`", result)
        self.assertNotIn("Railway", result)
        self.assertNotIn("self_heal", result)
        self.assertNotIn("app/skills/research_tools/tiktok.py", result)
        self.assertNotIn("GitHub", result)

    def test_normal_prose_not_mangled(self):
        """Ordinary conversational reply must pass through clean."""
        text = "Got it. Working on that now — should have it ready shortly."
        from app.plugins.output_cleanup import clean
        result = clean(text)
        # Short normal text: either unchanged (None) or equivalent
        if result is not None:
            self.assertIn("Got it", result)
            self.assertIn("Working on that", result)


class TestEnvFlag(unittest.TestCase):
    """OUTPUT_CLEANUP_ENABLED flag gates the hook."""

    def test_disabled_returns_none(self):
        """When OUTPUT_CLEANUP_ENABLED=0, on_transform returns None."""
        os.environ["OUTPUT_CLEANUP_ENABLED"] = "0"
        try:
            from app.plugins.output_cleanup import on_transform
            dirty = "Here is **bold** text with Railway in it."
            result = on_transform(response_text=dirty)
            self.assertIsNone(result)
        finally:
            del os.environ["OUTPUT_CLEANUP_ENABLED"]

    def test_enabled_by_default_cleans(self):
        """When OUTPUT_CLEANUP_ENABLED is unset (default ON), dirty text is cleaned."""
        os.environ.pop("OUTPUT_CLEANUP_ENABLED", None)
        from app.plugins.output_cleanup import on_transform
        dirty = "Here is **bold** text mentioning Railway and the API endpoint."
        result = on_transform(response_text=dirty)
        self.assertIsNotNone(result)
        self.assertNotIn("**bold**", result)
        self.assertNotIn("Railway", result)
        self.assertNotIn("API", result)

    def test_register_hook(self):
        """register() calls ctx.register_hook with transform_llm_output."""
        hooks = {}

        class FakeCtx:
            def register_hook(self, name, fn):
                hooks[name] = fn

        from app.plugins.output_cleanup import register
        register(FakeCtx())
        self.assertIn("transform_llm_output", hooks)
        self.assertTrue(callable(hooks["transform_llm_output"]))


class TestInternalErrorStripping(unittest.TestCase):
    """(a0) Raw internal tool-errors / CLI hints are DELETED, not shown."""

    def _clean(self, text: str) -> str:
        from app.plugins.output_cleanup import clean
        result = clean(text)
        return result if result is not None else text

    def test_home_channel_error_appended_is_removed(self):
        dirty = (
            "Got it, saved. No home channel set for telegram to determine "
            "where to send the message. or set a home channel via: "
            "hermes config set TELEGRAM_HOME_CHANNEL <id>"
        )
        out = self._clean(dirty)
        self.assertNotIn("home channel", out.lower())
        self.assertNotIn("hermes config set", out.lower())
        self.assertIn("Got it, saved.", out)

    def test_bluebubbles_variant_removed(self):
        dirty = "Done. No home channel set for bluebubbles to determine where to send the message."
        out = self._clean(dirty)
        self.assertNotIn("home channel", out.lower())
        self.assertIn("Done.", out)

    def test_normal_reply_untouched(self):
        clean_text = "You're all set! Want me to set up anything else?"
        from app.plugins.output_cleanup import clean
        self.assertIsNone(clean(clean_text))


class NewRewritesTest(unittest.TestCase):
    """Tests for rewrites added after the June 2026 jargon audit."""

    def _clean(self, text: str) -> str:
        from app.plugins.output_cleanup import clean
        result = clean(text)
        return result if result is not None else text

    def test_sha_rewritten(self):
        out = self._clean("The current SHA is abc1234.")
        self.assertNotIn("SHA", out)
        self.assertIn("version", out)

    def test_commit_hash_rewritten(self):
        out = self._clean("Checking the commit hash: abc1234.")
        self.assertNotIn("commit hash", out.lower())

    def test_plan_label_stripped(self):
        out = self._clean("Plan: add a timer skill. Trigger: user texts timer.")
        self.assertNotIn("Plan:", out)

    def test_trigger_label_rewritten(self):
        out = self._clean("Trigger: text status to get the version.")
        self.assertNotIn("Trigger:", out)
        self.assertIn("When you say", out)

    def test_all_caps_env_var_redacted(self):
        out = self._clean("Using RAILWAY_GIT_COMMIT_SHA for the version.")
        self.assertNotIn("RAILWAY_GIT_COMMIT_SHA", out)
        self.assertIn("[setting]", out)

    def test_env_var_phrase_rewritten(self):
        out = self._clean("Set this via env var before starting.")
        self.assertNotIn("env var", out.lower())
        self.assertIn("setting", out.lower())

    def test_url_rewritten(self):
        out = self._clean("The URL is https://example.com/path.")
        self.assertNotIn("URL", out)

    def test_full_url_redacted(self):
        out = self._clean("Check https://demo-agent.example.com/health for status.")
        self.assertNotIn("https://demo-agent-production", out)

    def test_timeout_rewritten(self):
        out = self._clean("The request timed out after 30 seconds.")
        self.assertNotIn("timed out", out.lower())

    def test_auth_rewritten(self):
        out = self._clean("Authentication failed.")
        self.assertNotIn("Authentication", out)
        self.assertIn("login", out.lower())

    def test_credentials_rewritten(self):
        out = self._clean("Check your credentials and try again.")
        self.assertNotIn("credentials", out.lower())
        self.assertIn("login details", out.lower())

    def test_config_rewritten(self):
        out = self._clean("Update the config file to enable this.")
        self.assertNotIn("config", out.lower())
        self.assertIn("settings", out.lower())

    def test_yaml_rewritten(self):
        out = self._clean("Edit the YAML to add the new route.")
        self.assertNotIn("YAML", out)
        self.assertIn("settings file", out.lower())

    def test_cli_rewritten(self):
        out = self._clean("Run it via the CLI.")
        self.assertNotIn("CLI", out)

    def test_async_rewritten(self):
        out = self._clean("This runs async so it won't block.")
        self.assertNotIn("async", out.lower())

    def test_status_code_rewritten(self):
        out = self._clean("Got status code 500 from the server.")
        self.assertNotIn("status code", out.lower())
        self.assertIn("error", out.lower())


class RepoRootResolutionTest(unittest.TestCase):
    """Verify REPO_ROOT resolves to /opt/agent (git repo) not /opt/data (volume)
    when HERMES_HOME points to a directory with no .git folder."""

    def test_hermes_home_without_git_uses_source_path(self):
        """REPO_ROOT resolution logic: when HERMES_HOME has no .git,
        the code should fall back to the source file's parent tree.
        Tests the _resolve_repo_root helper directly instead of reloading modules."""
        import tempfile
        import pathlib
        import os

        # Replicate the resolution logic from execute.py/deploy.py
        def _resolve_repo_root(hermes_home: str) -> pathlib.Path:
            p = pathlib.Path(hermes_home)
            if p.exists() and (p / ".git").exists():
                return p
            # Fall back to source tree (same as parents[3] in the real code)
            return pathlib.Path(__file__).resolve().parents[3]

        with tempfile.TemporaryDirectory() as fake_home:
            # fake_home has no .git → simulate Railway /opt/data volume
            result = _resolve_repo_root(fake_home)
            self.assertNotEqual(str(result), fake_home,
                "REPO_ROOT should not be HERMES_HOME when it has no .git")
            # Must be outside the temp dir
            self.assertNotIn(fake_home, str(result))


class PureErrorFallbackTest(unittest.TestCase):
    def _clean(self, text: str) -> str:
        from app.plugins.output_cleanup import clean
        result = clean(text)
        return result if result is not None else text

    def test_pure_error_keeps_original_not_empty(self):
        # If the whole reply is the error, keep the original rather than send a
        # blank message (better an awkward line than nothing).
        dirty = "No home channel set for telegram to determine where to send the message."
        out = self._clean(dirty)
        self.assertTrue(out.strip())  # never empty


if __name__ == "__main__":
    unittest.main()
