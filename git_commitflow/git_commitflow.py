#!/usr/bin/env python
#
# Copyright (c) 2020-2024 James Cherti
# URL: https://github.com/jamescherti/git-commitflow
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# this program. If not, see <https://www.gnu.org/licenses/>.
#
"""Readline manager."""

import argparse
import logging
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import List, Union

from colorama import Fore

from .cache_file import CacheFile
from .readline_manager import ReadlineManager

# TODO: Add configuration file for the following options:
# GIT_DIFF_OPTS = ['--', ':!*.asc', ':!*vault.yaml', ':!*vault.yml']
# MIN_COMMIT_MESSAGE_SIZE = 6
# IGNORE_FILENAMES_REGEX = ["^flycheck_", "^flymake_"]

GIT_DIFF_OPTS: List[str] = []
MIN_COMMIT_MESSAGE_SIZE = 1
GIT_COMMITFLOW_DATA_DIR = Path("~/.config/git-commitflow").expanduser()
CACHE_FILE = GIT_COMMITFLOW_DATA_DIR / "repo-data.json"
IGNORE_FILENAMES_REGEX: List[str] = []


def remove_matching_filenames(filenames: List[str],
                              patterns: List[str]) -> List[str]:
    """
    Remove filenames that match any of the given regex patterns.

    :param filenames: A list of filenames to filter.
    :param patterns: A list of regex patterns to match filenames against.
    :return: A list of filenames that do not match any of the patterns.
    """
    compiled_patterns = [re.compile(pattern) for pattern in patterns]
    filtered_filenames = [filename for filename in filenames
                          if not any(pattern.match(os.path.basename(filename))
                                     for pattern in compiled_patterns)]
    return filtered_filenames


def text_input(prompt: str,
               prompt_history_file: Union[os.PathLike, str, None] = "",
               default: str = "") -> str:
    prompt_history_file = str(prompt_history_file) + ".rl"
    logging.debug("[DEBUG] History file: %s", str(prompt_history_file))
    readline_manager = ReadlineManager(prompt_history_file)
    user_input = readline_manager.readline_input(prompt=prompt,
                                                 default=default)
    return user_input


class GitCommitFlow:
    def __init__(self):
        GIT_COMMITFLOW_DATA_DIR.mkdir(parents=True, exist_ok=True)

        self.args = self._parse_args()

        self.git_repo_dir = None
        self.find_git_repo_dir()

        self.amount_commits = self.count_commits()
        self.cache = CacheFile(CACHE_FILE)

        self.branch = self._get_first_line_cmd("git symbolic-ref --short HEAD")

    def _parse_args(self):
        """Parse command-line arguments."""
        usage = "%(prog)s [--option] [args]"
        parser = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                         usage=usage)
        parser.add_argument(
            "-p",
            "--push",
            default=False,
            action="store_true",
            required=False,
            help="Git push after a successful commit",
        )

        parser.add_argument(
            "-r",
            "--recursive",
            default=False,
            action="store_true",
            required=False,
            help="Execute this script against all submodules",
        )

        return parser.parse_args()

    def main(self):
        errno = 0

        if self.amount_commits > 0:
            if len(self._run("git --no-pager diff --name-only "
                             "--diff-filter=TXBU HEAD")) > 0:
                print("There is an issue in the repository "
                      f"'{self.git_repo_dir}'.")
                sys.exit(1)

        self.git_submodule_foreach()

        if self._run("git status --porcelain"):
            self.git_add()
            errno = self.git_ci()
        else:
            print("[COMMIT] Nothing to commit "
                  f"(Path: '{self.git_repo_dir}').")

        if not errno and self.args.push:
            try:
                self.cache.load()
                self.git_push()
            finally:
                self.cache.save()

        sys.exit(errno)

    def git_submodule_foreach(self):
        try:
            git_commit_wrapper_recursive = \
                int(os.environ.get("GIT_COMMIT_WRAPPER_RECURSIVE", "0"))
        except ValueError:
            git_commit_wrapper_recursive = 0

        if self.args.recursive or git_commit_wrapper_recursive:
            git_ci_script = Path(__file__).absolute()
            print(f"{Fore.LIGHTYELLOW_EX}[SUBMODULE FORREACH] "
                  f"{self.git_repo_dir}{Fore.RESET}")
            cmd = ["git", "submodule", "--quiet", "foreach", "--recursive",
                   str(git_ci_script)]
            if self.args.push:
                cmd += ["--push"]
            try:
                subprocess.check_call(cmd)
            except subprocess.CalledProcessError as proc_err:
                print(f"Error: {proc_err}", file=sys.stderr)
                sys.exit(1)

    def git_ci(self) -> int:
        """Function that performs the git commit."""
        print(f"{Fore.LIGHTYELLOW_EX}[GIT COMMIT] "
              f"{self.git_repo_dir}{Fore.RESET}")
        git_commit_opts = ["-a"]

        commit_message = self.diff_and_get_commit_message()
        if commit_message:
            git_commit_opts.extend(["-m", commit_message])
            print(f"Commit message: {commit_message}")
        else:
            git_commit_opts.extend(["--reset-author"])
            if self.amount_commits > 0:
                # Reuse the commit message of the previous commit
                git_commit_opts.extend(["--reuse-message=HEAD"])

        print("[RUN] git commit", " ".join(git_commit_opts))
        try:
            subprocess.check_call(["git", "commit"] + git_commit_opts)

            # TODO: maybe git show without a pager?
            # print()
            # subprocess.check_call(["git", "show"])

            print()
            print(Fore.GREEN + "[COMMIT] git commit was SUCCESSFUL." +
                  Fore.RESET)
        except subprocess.CalledProcessError:
            print()
            print(Fore.RED + "[COMMIT] git commit has FAILED." + Fore.RESET)
            return 1

        return 0

    def git_push(self):
        # --------------
        # Load cache
        # --------------
        remote_url = self._get_first_line_cmd("git ls-remote  --get-url")
        branch = self.branch
        git_push_commit_refs = self.cache.get("git_push_commit_refs", {})

        try:
            git_push_commit_refs[remote_url]
        except KeyError:
            git_push_commit_refs[remote_url] = {}

        try:
            git_push_commit_refs[remote_url][branch]
        except KeyError:
            git_push_commit_refs[remote_url][branch] = ""

        commit_ref = \
            self._get_first_line_cmd("git rev-parse --verify HEAD")

        if commit_ref == git_push_commit_refs[remote_url][branch]:
            print(f"[PUSH] Already pushed: " f"{self.git_repo_dir}")
            return True

        # -----------
        # GIT PUSH
        # -----------
        print(f"{Fore.LIGHTYELLOW_EX}[GIT PUSH] "
              f"{self.git_repo_dir}{Fore.RESET}")
        if not self._run(["git", "remote", "-v"]):
            return True  # No git remote

        try:
            # Show the remote branch that is tracked by the current local
            # branch The error message will be: fatal: no such branch: 'master'
            subprocess.check_call(["git", "rev-parse",
                                   "--symbolic-full-name", "HEAD@{u}"])

            subprocess.check_call(["git", "fetch", "-a"])
        except subprocess.CalledProcessError as proc_err:
            print(f"Error: {proc_err}", file=sys.stderr)
            return 1

        if subprocess.call(["git", "merge", "--ff-only"]) != 0:
            if self.confirm("Git failed to merge fast-forward."
                            "Do you want to run 'git pull --rebase'"):
                if subprocess.call(["git", "pull", "--rebase"]) != 0:
                    print("Error with 'git pull --rebase'...")
                    return 1

        print()
        print('[RUN] git push')

        success = False
        if subprocess.call(["git", "push"]) == 0:
            print()
            print(f"{Fore.GREEN}[PUSH] git commit and push were "
                  f"SUCCESSFUL.{Fore.RESET}")
            success = True
        else:
            print()
            print(f"{Fore.RED}[PUSH] git commit and push FAILED.{Fore.RESET}")

        # ------------------
        # Update cache file
        # ------------------
        if success:
            branch = self._get_first_line_cmd("git symbolic-ref --short HEAD")
            commit_ref = \
                self._get_first_line_cmd("git rev-parse --verify HEAD")
            git_push_commit_refs[remote_url][branch] = commit_ref
            self.cache.set("git_push_commit_refs", git_push_commit_refs)

        return success

    def git_config_get(self, git_var: str, default_value: str = "") -> str:
        try:
            return self._get_first_line_cmd(["git", "config", git_var])
        except subprocess.CalledProcessError:
            return default_value

    def find_git_repo_dir(self):
        try:
            self.git_repo_dir = Path(
                self._get_first_line_cmd("git rev-parse --show-toplevel")
            )
        except subprocess.CalledProcessError as proc_err:
            print(f"Error: {proc_err}", file=sys.stderr)
            sys.exit(1)

        if not self.git_repo_dir.is_dir():
            print(f"Error: The Git repository '{self.git_repo_dir}' "
                  "is not a directory", file=sys.stderr)
            sys.exit(1)

    def count_commits(self):
        return len(self._run("git rev-list --all --count"))

    def _get_first_line_cmd(self, cmd) -> str:
        output = self._run(cmd)
        try:
            return output[0]
        except IndexError:
            return ""

    def _run(self, command: Union[str, List[str]]) -> List[str]:
        if isinstance(command, str):
            command = shlex.split(command)
        result = subprocess.run(command, stdout=subprocess.PIPE,
                                check=True, text=True)
        return result.stdout.splitlines()

    def git_add(self):
        list_untracked_files = self._run(["git", "-C", self.git_repo_dir,
                                          "ls-files", "--others",
                                          "--exclude-standard"])
        list_untracked_files = remove_matching_filenames(
            list_untracked_files,
            IGNORE_FILENAMES_REGEX,
        )
        if list_untracked_files:
            print("Git repository:", self.git_repo_dir)
            print("\nFiles:")
            print("------")
            print(list_untracked_files)
            print()
            while True:
                answer = input("git add? [y,n] ")
                if answer.lower() == "y":
                    self._run(["git", "add"] + list_untracked_files)
                    break

                if answer.lower() == "n":
                    break

    def diff_and_get_commit_message(self) -> str:
        prompt_history_file = None
        git_common_dir = \
            self._get_first_line_cmd("git rev-parse --git-common-dir").strip()

        if git_common_dir:
            prompt_history_file = \
                Path(git_common_dir).joinpath("git-commitflow-history")

        if self.amount_commits > 0:
            # Diff against HEAD shows both staged and unstaged changes
            cmd = ["git", "--paginate", "diff",
                   "--diff-filter=d", "--color"] + ["HEAD"] + GIT_DIFF_OPTS
            subprocess.check_call(cmd)

        subprocess.check_call(["git", "status"])
        print(f"Git repo: {Fore.YELLOW}{self.git_repo_dir}{Fore.RESET}")
        print()

        git_name = self.git_config_get("user.name", "Unknown")
        git_email = self.git_config_get("user.email", "unknown@domain.ext")
        git_author = f"{git_name} <{git_email}>"

        print(f"Author: {Fore.YELLOW + git_author + Fore.RESET} ")
        print("Branch:", Fore.YELLOW + self.branch + Fore.RESET)
        print("Git message: ", end="")

        commit_message = self.git_config_get("custom.commit-message").strip()
        previous_message = ""
        if commit_message:
            print(Fore.YELLOW + commit_message + Fore.RESET)
        elif self.amount_commits > 0:
            previous_message = \
                "\n".join(
                    self._run("git --no-pager log -1 --pretty=%B")).rstrip()
            print(Fore.YELLOW + previous_message + Fore.RESET)

        commit_message = self.prompt_git_commit_message(
            commit_message,
            prompt_history_file=prompt_history_file,
        )

        # TODO: move this to a function
        logging.debug("[DEBUG] Previous message: %s", previous_message)
        logging.debug("[DEBUG] Commit message: %s", commit_message)
        if prompt_history_file and not commit_message and previous_message:
            with open(prompt_history_file, "a", encoding="utf-8") as fhandler:
                fhandler.write(f"{previous_message}\n")

        return commit_message

    def prompt_git_commit_message(
            self, commit_message: str,
            prompt_history_file: Union[str, None, os.PathLike]) -> str:
        while True:
            try:
                commit_message = text_input(
                    "Commit message: ",
                    prompt_history_file=prompt_history_file,
                )
            except (EOFError, KeyboardInterrupt):
                sys.exit(0)

            if len(commit_message) > 0 and \
               len(commit_message) <= MIN_COMMIT_MESSAGE_SIZE:
                print("Error: the commit message is too short.")
                print()
            else:
                break

        return commit_message

    @staticmethod
    def confirm(prompt: str) -> bool:
        while True:
            try:
                answer = input(f"{prompt} [y,n] ")
            except KeyboardInterrupt:
                print()
                sys.exit(1)

            if answer not in ["y", "n"]:
                continue

            return bool(answer == "y")