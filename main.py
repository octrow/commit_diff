import argparse
import os
import logging
import re
import tempfile
import traceback
from loguru import logger
import git

from OCDG.commit_history import CommitHistory
from clients import create_client
from config import load_configuration

from git_repo import GitAnalyzer
from OCDG.utils import run_git_command, generate_commit_description

# Global variable to store log file path
COMMIT_MESSAGES_LOG_FILE = "commit_messages.log"

# Define file/folder paths and patterns to ignore ENTIRE SECTIONS
IGNORED_SECTION_PATTERNS = {
    r'venv.*',  # Ignore any path containing 'venv'
    r'.idea.*'  # Ignore any path containing '.idea'
    r'node_modules.*',  # Ignore any path containing 'node_modules'
    r'__pycache__.*',  # Ignore any path containing '__pycache__
}

# Define file extensions and patterns to ignore
IGNORED_LINE_PATTERNS = {
    r'.*\.(png|jpg|jpeg|gif|bmp|tiff|svg|ico|raw|psd|ai)$',
    r'.*\.(xlsx|xls|docx|pptx|pdf)$', r'.*\.(pack|idx|DS_Store|sys|ini|bat|plist)$',
    r'.*\.(exe|dll|so|bin)$', r'.*\.(zip|rar|7z|tar|gz|bz2)$',
    r'.*\.(mp3|wav|aac|flac)$', r'.*\.(mp4|avi|mov|wmv|flv)$',
    r'.*\.(db|sqlitedb|mdb)$', r'.*\.(ttf|otf|woff|woff2)$',
    r'.*\.(tmp|temp|swp|swo)$', r'.*\.(o|obj|pyc|class)$',
    r'.*\.(cer|pem|crt|key)$', r'.*\.(conf|cfg|config)$',
    r'.*\.(env)$', r'node_modules', r'.*\.(pyo)$',
    r'(package-lock\.json|poetry\.lock|yarn\.lock|Gemfile\.lock)',
    r'.*\.(err|stderr|stdout|log)$', r'.*\.(cache|cached)$'
}

def user_confirms_rewrite(commit_history):
    """Presents the proposed changes to the user and asks for confirmation."""
    print("\nThe following commit messages will be rewritten:")
    print("-" * 80)
    for commit in commit_history.commits:
        if commit.new_message:
            print(f"Commit: {commit.hash} (Author: {commit.author})")
            print(f"Old: {commit.message}")
            print(f"New: {commit.new_message}")
            print("-" * 80)

    while True:
        confirmation = input("Do you want to rewrite these commit messages? (yes/no): ").lower()
        if confirmation in ("yes", "y"):
            return True
        elif confirmation in ("no", "n"):
            return False
        else:
            print("Invalid input. Please enter 'yes' or 'no'.")

class RepositoryUpdater:
    """Handles safe rewriting of commit messages."""

    def __init__(self, repo_path: str):
        self.repo_path = repo_path
        self.refs_backup_file = os.path.join(self.repo_path, ".git", "refs_backup")

    def backup_refs(self):
        """Backs up refs using git for-each-ref to a file."""
        try:
            run_git_command(
                [
                    "for-each-ref",
                    "--format='%(refname)'",
                    "refs/heads/",
                    "refs/remotes/",
                    "refs/tags/",
                    ">",
                    self.refs_backup_file,
                ],
                self.repo_path,
            )
            logging.info("Backed up refs to '.git/refs_backup'")
        except Exception as e:
            logging.error(f"Error backing up refs: {e}")
            raise

    def restore_refs(self):
        """Restores refs from the backup file."""
        try:
            if not os.path.exists(self.refs_backup_file):
                logging.warning(
                    f"Refs backup file '{self.refs_backup_file}' not found. Skipping restore."
                )
                return
            run_git_command(
                ["update-ref", "--stdin", "<", self.refs_backup_file], self.repo_path
            )
            logging.info("Restored refs from backup.")
        except Exception as e:
            logging.error(f"Error restoring refs: {e}")
            raise
        finally:
            # Clean up the backup file after restore attempt
            if os.path.exists(self.refs_backup_file):
                os.remove(self.refs_backup_file)

    def rewrite_commit_messages(self, commit_history):
        """Rewrites commit messages using git filter-branch."""
        with tempfile.TemporaryDirectory() as temp_dir:
            filter_script_path = os.path.join(temp_dir, "filter_script.py")
            self.generate_filter_script(commit_history, filter_script_path)

            try:
                self.backup_refs()  # Backup before rewriting!

                filter_branch_cmd = [
                    "filter-branch",
                    "-f",
                    "--msg-filter",
                    f"python {filter_script_path}",
                    "--tag-name-filter",
                    "cat",
                    "--",
                    "HEAD",
                ]
                run_git_command(filter_branch_cmd, self.repo_path)
                logging.info("Commit messages rewritten successfully.")

            except Exception as e:
                logging.error(f"Error rewriting commit messages: {e}")
                self.restore_refs()  # Attempt restore on error
                raise
            finally:
                backup_file = os.path.join(self.repo_path, ".git/refs_backup")
                if os.path.exists(backup_file):
                    os.remove(backup_file)

    def generate_filter_script(self, commit_history, script_path):
        """Generates the Python script for git filter-branch."""
        with open(script_path, "w") as f:
            f.write(
                """
import sys

def filter_message(message):
    message_map = {
"""
            )
            for commit in commit_history.commits:
                if commit.new_message:
                    # Properly escape single quotes in new_message
                    escaped_message = commit.new_message.replace("'", r"\'")
                    f.write(f"        '{commit.hash}': '{escaped_message}',\n")
            f.write(
                """
    }
    commit_hash = sys.stdin.readline().strip()
    return message_map.get(commit_hash, message)

if __name__ == "__main__":
    print(filter_message(sys.stdin.readline().strip()))
"""
            )

def save_commit_messages_to_log(commit_history: CommitHistory):
    """Saves old and new commit messages to the log file."""
    try:
        with open(COMMIT_MESSAGES_LOG_FILE, "a") as log_file:
            for commit in commit_history.commits:
                if commit.new_message:
                    log_file.write(f"Commit: {commit.hash}\n")
                    log_file.write(f"Old Message: {commit.message}\n")
                    log_file.write(f"New Message: {commit.new_message}\n\n")
        logging.info(f"Old and new commit messages saved to '{COMMIT_MESSAGES_LOG_FILE}'")
    except Exception as e:
        logging.error(f"Failed to save commit messages to log file: {e}")

def filter_diff(diff: str) -> str:
    """Removes unwanted lines from the diff based on file extensions and patterns."""
    filtered_lines = []
    skip_section = False  # Flag to skip entire diff sections

    for line in diff.splitlines():
        # Section-Level Filtering
        if line.startswith('diff --git '):
            if any(re.search(pattern, line) for pattern in IGNORED_SECTION_PATTERNS):
                skip_section = True
                logging.info(f"Skipping section: {line}")
                continue
            else:
                skip_section = False  # Reset the flag for the new section
                filtered_lines.append(line) # Add the 'diff --git' line if not skipped
        else:
            # Line-Level Filtering (only if not skipping the section)
            if not skip_section:
                # Extract potential file paths (using the improved regex from before)
                match = re.search(r'^(?:\+\+\+ |--- |diff --git |index |Binary files )?(.*?)[ \t]', line)
                if match:
                    file_path = match.group(1).strip()
                    if any(re.search(pattern, file_path) for pattern in IGNORED_LINE_PATTERNS):
                        logging.info(f"Skipping line: {line}")
                        continue  # Skip the line
                filtered_lines.append(line)

    return "\n".join(filtered_lines)

def main():
    # Parse command-line arguments
    parser = argparse.ArgumentParser(description="Revitalize old commit messages using LLMs.")
    parser.add_argument("repo_path", help="Path to the Git repository (local path or URL).")
    parser.add_argument("-b", "--backup_dir",
                        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "backup"),
                        help="Directory for repository backup.")
    parser.add_argument("-l", "--llm", choices=["openai", "groq", "replicate"], default="openai", help="Choice of LLM.")
    parser.add_argument("-m", "--model", default="meta/llama3-70b-instruct", help="Choice of LLM model.")
    parser.add_argument("-f", "--force-push", action="store_true", help="Force push to remote after rewrite.")
    parser.add_argument(
        "-r",
        "--restore",
        action="store_true",
        help="Restore refs from backup before proceeding.",
    )
    # Add more arguments as needed...
    args = parser.parse_args()

    # Configure logging
    # logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

    # Load configuration
    config = load_configuration()
    os.makedirs(config['COMMIT_DIFF_DIRECTORY'], exist_ok=True)

    # Determine repository type and get URL
    if args.repo_path.startswith(("http", "git@")):
        repo_url = args.repo_path
        repo_path = os.path.join(config['COMMIT_DIFF_DIRECTORY'], os.path.basename(repo_url).replace(".git", ""))
        # Clone if the directory doesn't exist
        if not os.path.exists(repo_path):
            logging.info(f"Cloning remote repository to {repo_path}")
            git.Repo.clone_from(repo_url, repo_path)
    else:
        # Get absolute path for local repositories
        repo_path = os.path.abspath(args.repo_path)
        logging.info(f"Using local repository path: {repo_path}")
        repo_url = GitAnalyzer.get_repo_url(repo_path)
        if not repo_url:
            logging.error("Failed to get repository URL from local path. Exiting...")
            return

    # 1. Backup Repository
    updater = RepositoryUpdater(repo_path)
    # Restore from backup if requested
    if args.restore:
        try:
            print("Attempting to restore refs from backup...")
            updater.restore_refs()
            print("Restore complete. Exiting.")
            return  # Exit after restore
        except Exception as e:
            logging.critical(f"Error during restore: {e}")
            return  # Exit on restore error
    # BACKUP REFS IMMEDIATELY AFTER LOADING REPOSITORY
    try:
        logging.info("Backing up refs before any operations...")
        updater.backup_refs()
    except Exception as e:
        logging.critical(f"Error during initial backup: {e}")
        return  # Exit on backup error

    # 2. Load Commit History
    logging.info("Loading commit history...")
    try:
        analyzer = GitAnalyzer(repo_path)
        commits = analyzer.get_commits() # Now get commits with diffs
        # Get the repo object from the analyzer
        repo = analyzer.repo

        commit_history = CommitHistory()
        commit_history.commits = commits  # Assign the commits to the history object
        counter = 0
        for commit in commits:
            logger.info(f"Commit {counter}: {commit.hash}")
            counter += 1
    except Exception as e:
        logging.error(f"Failed to load commit history: {e}")
        return

    logging.info(f"Loaded {len(commits)} commits from repository.")

    # 3. Initialize LLM Interface
    client = create_client(args.llm, config)
    logging.info(f"Initialized LLM client: {client}")

    # 4. Process each commit
    initial_commit_hash = run_git_command(['rev-list', '--max-parents=0', 'HEAD'], repo_path).strip()
    for i, commit in enumerate(commits):
        logging.info(f"Processing commit {i + 1}/{len(commits)}: {commit.hash}")
        try:
            if commit.hash == initial_commit_hash:
                logging.info(f"Skipping diff for initial commit: {commit.hash}")
                diff = ""  # Or handle the initial commit differently
            else:
                diff = analyzer.get_commit_diff(commit.hash) # Fetch diff here
            filtered_diff = filter_diff(diff)
            new_message = generate_commit_description(
                filtered_diff, commit.message, client, args.model
            )

            if new_message is None:
                logging.warning(
                    f"Skipping commit {commit.hash} - No new message generated"
                )
                continue

            commit.new_message = new_message  # Store the new message

        except Exception as e:
            logging.error(
                f"Error processing commit {commit.hash}: {traceback.format_exc()} {e}"
            )
            return

    # 5. User Confirmation before Rewrite
    if user_confirms_rewrite(commit_history):
        # updater = RepositoryUpdater(repo_path)
        try:
            logging.info("Rewriting commit messages...")
            save_commit_messages_to_log(commit_history)
            updater.rewrite_commit_messages(commit_history)
        except Exception as e:
            logging.critical(
                f"An error occurred during the rewrite process. "
                f"'python {__file__} --restore'. Error: {e}"
            )
            return  # Stop execution after error

        # 6. Force push (if enabled and user is aware)
        if args.force_push:
            print(
                "\nWARNING: Force pushing will overwrite the remote repository's history!"
            )
            while True:
                force_confirm = input(
                    "Are you absolutely sure you want to force push? (yes/no): "
                ).lower()
                if force_confirm in ("yes", "y"):
                    logging.info(
                        "Force pushing changes to remote repository..."
                    )
                    try:
                        repo.git.push(
                            "--force-with-lease",
                            "origin",
                            repo.active_branch.name,
                        )
                        logging.info("Successfully force-pushed changes.")
                        break  # Exit confirmation loop
                    except Exception as e:
                        logging.error(f"Error force pushing changes: {e}")
                        return  # Stop execution after error
                elif force_confirm in ("no", "n"):
                    logging.info("Force push cancelled.")
                    break  # Exit confirmation loop
                else:
                    print("Invalid input. Please enter 'yes' or 'no'.")
    else:
        logging.info("Rewrite cancelled by user.")

    logging.info("OCDG process completed!")

if __name__ == "__main__":
    main()