"""The bash -> Python handoff."""

from __future__ import annotations

import unittest

from putio.config import REDACTED, SECRET_FIELDS, Config


class TestDefaults(unittest.TestCase):
    def test_empty_environment_yields_bash_defaults(self):
        config = Config.from_env({})
        # Not bash's "putio:": the account root is never a safe default for
        # something that deletes what it downloads.
        self.assertEqual(config.remote, "putio:Drovie/")
        self.assertEqual(config.rclone_transfers, 4)
        self.assertEqual(config.rclone_checkers, 8)
        self.assertEqual(config.rclone_buffer_size, "64M")
        self.assertEqual(config.rclone_multi_threads, 2)
        self.assertEqual(config.rclone_max_concurrent, 1)
        self.assertEqual(config.rclone_bwlimit, "off")
        self.assertEqual(config.rclone_stats_interval, "5s")
        # server_config_template is the sole source of this default; the
        # processor supplies none of its own.
        self.assertEqual(config.min_free_space_gb, 5)

    def test_empty_string_is_treated_as_unset(self):
        """Bash cannot pass a real ``None``; empty has to mean 'not configured'."""
        self.assertEqual(Config.from_env({"REMOTE": ""}).remote, "putio:Drovie/")
        self.assertEqual(Config.from_env({"RCLONE_TRANSFERS": ""}).rclone_transfers, 4)

    def test_values_override_defaults(self):
        config = Config.from_env({"REMOTE": "other:", "RCLONE_TRANSFERS": "9"})
        self.assertEqual(config.remote, "other:")
        self.assertEqual(config.rclone_transfers, 9)


class TestRemote(unittest.TestCase):
    def test_account_root_passes_through(self):
        """Existing configs that name the whole account keep working."""
        self.assertEqual(Config.from_env({"REMOTE": "putio:"}).remote, "putio:")

    def test_trailing_slash_passes_through(self):
        self.assertEqual(Config.from_env({"REMOTE": "putio:/"}).remote, "putio:/")
        self.assertEqual(Config.from_env({"REMOTE": "putio:Drovie/"}).remote, "putio:Drovie/")

    def test_bare_folder_gains_a_slash(self):
        """"$REMOTE$FILE" must not glue the folder and the file name together."""
        self.assertEqual(Config.from_env({"REMOTE": "putio:Drovie"}).remote, "putio:Drovie/")
        self.assertEqual(Config.from_env({"REMOTE": "putio:A/B"}).remote, "putio:A/B/")


class TestCoercion(unittest.TestCase):
    def test_use_temp_dir_needs_the_literal_true(self):
        """Bash tests it as ``[ "${USE_TEMP_DIR:-false}" = "true" ]``.

        Only that exact byte string enables staging, so a friendlier reading
        would have Python write to a staging volume bash never used — and one
        validate_config never checked for writability.
        """
        self.assertTrue(Config.from_env({"USE_TEMP_DIR": "true"}).use_temp_dir)
        for text in ("TRUE", "yes", "1", "on", "false", "0", "anything else"):
            self.assertFalse(Config.from_env({"USE_TEMP_DIR": text}).use_temp_dir, text)

    def test_delete_after_download_is_off_only_on_the_literal_false(self):
        """Bash tests it as ``[ "${DELETE_AFTER_DOWNLOAD}" != "false" ]``.

        The opposite convention to USE_TEMP_DIR, in the same config file. This
        one governs deleting from put.io, which is the durable work queue, so
        the two directions of error are not equivalent.
        """
        self.assertFalse(
            Config.from_env({"DELETE_AFTER_DOWNLOAD": "false"}).delete_after_download
        )
        for text in ("true", "TRUE", "yes", "0", "no", "FALSE", "anything else"):
            self.assertTrue(
                Config.from_env({"DELETE_AFTER_DOWNLOAD": text}).delete_after_download, text
            )

    def test_flag_defaults_match_bash(self):
        config = Config.from_env({})
        self.assertFalse(config.use_temp_dir)
        self.assertTrue(config.delete_after_download)

    def test_a_malformed_number_falls_back_rather_than_raising(self):
        """A typo in the config must not be able to stop a run."""
        self.assertEqual(Config.from_env({"MAX_RETRIES": "three"}).max_retries, 3)
        self.assertEqual(Config.from_env({"MIN_FREE_SPACE_GB": ""}).min_free_space_gb, 5)

    def test_pause_file_defaults_to_the_home_marker(self):
        """Bash applies this default ~2,300 lines below the handoff, so an
        early caller would see an empty string rather than the real path."""
        import os

        config = Config.from_env({})
        self.assertEqual(config.pause_file, os.path.expanduser("~/.putio_paused"))
        self.assertEqual(Config.from_env({"PAUSE_FILE": "/x/y"}).pause_file, "/x/y")

    def test_log_level_is_normalised(self):
        self.assertEqual(Config.from_env({"LOG_LEVEL": "debug"}).log_level, "DEBUG")


class TestProgressStateDir(unittest.TestCase):
    """The one place where empty is a setting, not an absence.

    ``PROGRESS_STATE_DIR=""`` switches progress publishing off in the bash
    script.  Defaulting it back on would start writing files an operator asked
    us not to write, and the readers — putioctl, the console and putio-statusd
    — would then draw bars for a run nobody expected to be publishing.
    """

    def test_absent_takes_the_default(self):
        config = Config.from_env({})
        self.assertEqual(config.progress_state_dir, "/tmp/putio_progress")
        self.assertTrue(config.progress_enabled)

    def test_present_but_empty_disables_it(self):
        config = Config.from_env({"PROGRESS_STATE_DIR": ""})
        self.assertIsNone(config.progress_state_dir)
        self.assertFalse(config.progress_enabled)

    def test_a_path_is_kept(self):
        config = Config.from_env({"PROGRESS_STATE_DIR": "/var/run/putio"})
        self.assertEqual(config.progress_state_dir, "/var/run/putio")
        self.assertTrue(config.progress_enabled)


class TestSecrets(unittest.TestCase):
    def test_redacted_hides_every_secret_value(self):
        """Driven off SECRET_FIELDS, not a hand-written list.

        A credential added to the handoff and forgotten here would be printed
        by config-dump, which is the one subcommand whose whole job is to print
        the configuration.
        """
        planted = {}
        env = {}
        for index, field in enumerate(sorted(SECRET_FIELDS)):
            value = "secret-value-{0}".format(index)
            planted[field] = value
            env[dict(Config._FIELDS)[field]] = value

        data = Config.from_env(env).redacted()
        for field, value in planted.items():
            self.assertEqual(data[field], REDACTED, field)
            self.assertNotIn(value, repr(data), field)

    def test_an_unset_secret_stays_visibly_unset(self):
        """Redacting an empty key would read as 'a key is configured'."""
        data = Config.from_env({}).redacted()
        for field in SECRET_FIELDS:
            self.assertEqual(data[field], "", field)

    def test_redacted_covers_every_field(self):
        data = Config.from_env({}).redacted()
        self.assertEqual(len(data), len(Config.env_names()))

    def test_secret_field_names_exist(self):
        for name in SECRET_FIELDS:
            self.assertIn(name, Config.from_env({}).redacted())


class TestRateLimitDelay(unittest.TestCase):
    """bash hands this straight to `sleep`, which takes a fraction."""

    def test_a_fractional_delay_survives(self):
        self.assertEqual(Config.from_env({"RATE_LIMIT_DELAY": "0.2"}).rate_limit_delay, 0.2)

    def test_an_unset_or_malformed_delay_falls_back(self):
        for value in ("", "   ", "nonsense"):
            self.assertEqual(
                Config.from_env({"RATE_LIMIT_DELAY": value}).rate_limit_delay, 1.0, value
            )


class TestContract(unittest.TestCase):
    """``env_names()`` is what the shell harness compares PUTIO_PY_VARS to."""

    def test_names_are_unique(self):
        names = Config.env_names()
        self.assertEqual(len(names), len(set(names)))

    def test_every_field_has_an_environment_name(self):
        from dataclasses import fields

        self.assertEqual(
            sorted(f.name for f in fields(Config)),
            sorted(field for field, _ in Config._FIELDS),
        )

    def test_names_look_like_shell_variables(self):
        import re

        for name in Config.env_names():
            self.assertRegex(name, r"^[A-Z][A-Z0-9_]*$")


if __name__ == "__main__":
    unittest.main()


class TestWebhookAlias(unittest.TestCase):
    def test_old_names_still_configure_the_webhook(self):
        config = Config.from_env(
            {"PLEX_REQUEST_WEBHOOK_URL": "http://d/hook", "PLEX_REQUEST_WEBHOOK_KEY": "k"}
        )
        self.assertEqual(config.drovie_webhook_url, "http://d/hook")
        self.assertEqual(config.drovie_webhook_key, "k")

    def test_new_names_win(self):
        config = Config.from_env(
            {
                "PLEX_REQUEST_WEBHOOK_URL": "http://old/hook",
                "DROVIE_WEBHOOK_URL": "http://new/hook",
            }
        )
        self.assertEqual(config.drovie_webhook_url, "http://new/hook")

    def test_the_key_is_redacted_under_its_new_name(self):
        self.assertIn("drovie_webhook_key", SECRET_FIELDS)
