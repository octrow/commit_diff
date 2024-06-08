import logging
import subprocess
import os
from loguru import logger
from typing import Any, List, Dict

def run_git_command(command, repo_path="."):
    """Executes a Git command and returns the output."""
    logger.debug(f"Running git command: git {command}")
    result = subprocess.run(["git"] + command, cwd=repo_path, capture_output=True, text=True, check=True, encoding="cp437")
    try:
        # result = subprocess.run(["git"] + command, cwd=repo_path, capture_output=True, text=True, check=True)
        result.check_returncode()
        output = result.stdout
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Git command failed: {e.stderr}") from e
    except UnicodeDecodeError:
        # If decoding as UTF-8 fails, try decoding as ISO-8859-1 (Latin-1) instead
        output = result.stdout.decode('ISO-8859-1')
    return output

def validate_repo_path(repo_path):
    """Checks if the provided path is a valid Git repository."""
    if not os.path.isdir(repo_path):
        raise ValueError(f"Invalid repository path: {repo_path}")
    try:
        run_git_command(["rev-parse", "--is-inside-work-tree"], repo_path)
    except RuntimeError:
        raise ValueError(f"{repo_path} is not a valid Git repository.")

def log_message(message, level="info"):
    """Logs a message with the specified severity level."""
    log_function = getattr(logging, level)
    log_function(message)

def find_closing_brace_index(text: str) -> int:
    """Finds the index of the closing brace that matches the first opening brace."""
    count = 0
    for i, char in enumerate(text):
        if char == '{':
            count += 1
        elif char == '}':
            count -= 1
            if count == 0:
                return i
    raise ValueError("Unbalanced curly braces")

def parse_output_string(output_string: str) -> dict:
    """Parses the output string generated by the AI model into a dictionary."""
    data = {}
    patterns = {
        'short_analysis': r'\*\*Short analysis\*\*: (.+?)\n',
        'commit_title': r'\*\*New Commit Title\*\*: (.+?)\n',
        'detailed_commit_message': r'\*\*New Detailed Commit Message\*\*:\n(.+?)\n\n\*\*Code Changes\*\*:',
        'code_changes': r'\*\*Code Changes\*\*:\n```\n(\{.+\})\n```'
    }

    for key, pattern in patterns.items():
        match = re.search(pattern, output_string, re.DOTALL)
        if match:
            if key == 'code_changes':
                try:
                    data[key] = json.loads(match.group(1))
                except json.JSONDecodeError as e:
                    logger.error(f"Error decoding JSON: {e} - {match.group(1)}")
                    return {}
            else:
                data[key] = match.group(1).strip()
    return data

def generate_commit_multi(diff: str, commit_message: str, client: Any, model: str) -> List[Dict[str, str]]:
    """Splits a diff into chunks and generates a commit message for each chunk."""
    diff_chunks = [diff[i:i + 6000] for i in range(0, len(diff), 6000)]
    commit_messages = []
    for i, diff_chunk in enumerate(diff_chunks):
        system_prompt = (
            f""""
            You are a helpful AI assistant that generates commit messages based on code changes and previous descriptions. Follow the commit guidelines of the GitHub repository.
            Previous commit message: {commit_message}
            Code changes: {'(partial)' if i != len(diff_chunks) - 1 else ''} 
            ```
            {diff_chunk}
            ```
            Generate a new commit message based on these changes. Output only in JSON Format
            {{
            "Short analysis": "str",
            "New Commit Title": "str",
            "New Detailed Commit Message": "str",
            "Code Changes": {{"filename": "str", "filename2": "str"}}
            }}
            """
        )
        chat_completion = client.generate_text(
            system_prompt,
            model=model
        )
        try:
            commit_messages.append(json.loads(chat_completion))
        except json.JSONDecodeError as e:
            logger.error(f"Error decoding JSON: {e} - {chat_completion}")
    return commit_messages

def combine_messages(multi_commit: List[Dict[str, str]], client: Any, model: str) -> dict:
    """Combines multiple commit messages into a single commit message."""
    prompt = f"""Combine the following messages into a single commit message in JSON format:
    ```json
    {json.dumps(multi_commit)}
    ```
    Output only in JSON Format
    {{
    "Short analysis": "str",
    "New Commit Title": "str",
    "New Detailed Commit Message": "str",
    "Code Changes": {{"filename": "str", "filename2": "str"}}
    }}
    """
    combined_message = client.generate_text(
        prompt,
        model=model
    )
    try:
        return json.loads(combined_message)
    except json.JSONDecodeError as e:
        logger.error(f"Error decoding JSON: {e} - {combined_message}")
        return {}

def generate_commit_description(diff: str, old_description: str, client: Any, model: str) -> str:
    """Generates a commit description for a potentially large diff."""
    try:
        if len(diff) >= 6000:
            logger.info("Diff is too long. Start splitting it into chunks.")
            multi_commit = generate_commit_multi(diff, old_description, client, model)
            if not multi_commit:
                logger.warning("Failed to generate multi-commit message. Skipping...")
                return None
            generated_message = combine_messages(multi_commit, client, model)
        else:
            system_prompt = (
                f""""
                You are a helpful AI assistant that generates commit messages based on code changes and previous descriptions. Follow the commit guidelines of the GitHub repository.
                Previous commit message: {old_description}
                Code changes: 
                ```
                {diff}
                ```
                Generate a new commit message based on these changes. Output only in JSON Format
                {{
                "Short analysis": "str",
                "New Commit Title": "str",
                "New Detailed Commit Message": "str",
                "Code Changes": {{"filename": "str", "filename2": "str"}}
                }}
                """
            )
            chat_completion = client.generate_text(
                system_prompt,
                model=model
            )
            generated_message = json.loads(chat_completion)
        new_description = "\n".join(
            [
                generated_message.get("New Commit Title", ""),
                "",  # Add an empty line between title and body
                generated_message.get("New Detailed Commit Message", ""),
            ]
        ).strip()
        return new_description
    except Exception as e:
        logger.error(f"Error generating commit description: {e}")
        return None