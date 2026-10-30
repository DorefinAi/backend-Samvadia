"""Loaded before any test module imports the app: the owner API (/owner/*) is mounted only when OWNER_API_KEY is set
at start-up, so tests set a throwaway key here. Tests that need it off or different change the setting at run time;
the OwnerGate middleware reads it on every request."""
import os

TEST_OWNER_KEY = "owner-key-for-tests-0123456789-abcdefghij"  # 40 chars, tests only
os.environ["OWNER_API_KEY"] = TEST_OWNER_KEY
