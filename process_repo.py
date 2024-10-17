import os
import re
import sys
import psycopg2
from pgconf_utils import ask_openai, ask_ubicloud, OPENAI_CONTEXT_WINDOW, UBICLOUD_CONTEXT_WINDOW
from dotenv import load_dotenv
from backfill_embeddings import backfill
load_dotenv()

FILE_PROMPT = """Here is some code. Summarize what the code does."""
FILE_SUMMARIES_PROMPT = """Here are multiple summaries of sections of a file. Summarize what the code does."""
FOLDER_PROMPT = """Here are the summaries of the files and subfolders in this folder. Summarize what the folder does."""
FOLDER_SUMMARIES_PROMPT = """Here are multiple summaries of the files and subfolders in this folder. Summarize what the folder does."""
REPO_PROMPT = """Here are the summaries of the folders in this repository. Summarize what the repository does."""
COMMIT_PROMPT = """Here is a commit, including the commit message, and the changes made in the commit. Summarize the commit."""

# Database
DATABASE_URL = os.getenv("DATABASE_URL")
conn = psycopg2.connect(DATABASE_URL)
cur = conn.cursor()


def is_acceptable_file(file_name):
    ACCEPTABLE_SUFFIXES = [
        '.py', '.js', '.java', '.rb', '.go', '.rs', '.json',
        '.yaml', '.yml', '.xml', '.md', '.txt', '.sh', '.sql', '.ts'
    ]
    ACCEPTABLE_FILENAMES = {'Makefile', 'Dockerfile'}
    return (
        any(file_name.endswith(suffix) for suffix in ACCEPTABLE_SUFFIXES) or
        file_name in ACCEPTABLE_FILENAMES
    )


def is_acceptable_folder(folder_name):
    EXCLUDED_DIRS = {'.git', '.devcontainer', '.venv', 'node_modules', }
    path_parts = folder_name.split(os.sep)
    return not any(part in EXCLUDED_DIRS for part in path_parts)


def insert_repo(repo_name):
    INSERT_REPO = f"""INSERT INTO repos ("name") VALUES (%s) ON CONFLICT DO NOTHING;"""
    cur.execute(INSERT_REPO, (repo_name,))
    conn.commit()


def insert_folder(folder_name, repo_name, llm_openai, llm_ubicloud):
    INSERT_FOLDER = """
        INSERT INTO folders ("name", "repo", "llm_openai", "llm_ubicloud")
        VALUES (%s, %s, %s, %s) ON CONFLICT ("name", "repo") DO NOTHING;
    """
    cur.execute(INSERT_FOLDER, (folder_name, repo_name,
                llm_openai.strip(), llm_ubicloud.strip()))
    conn.commit()


def insert_file(file_name, folder_name, repo_name, file_content, llm_openai, llm_ubicloud):
    INSERT_FILE = """
        INSERT INTO files ("name", "folder", "repo", "code", "llm_openai", "llm_ubicloud")
        VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT ("name", "folder", "repo") DO NOTHING;
    """
    cur.execute(INSERT_FILE, (file_name, folder_name, repo_name,
                file_content, llm_openai.strip(), llm_ubicloud.strip()))
    conn.commit()


def insert_commit(repo_name, commit_id, author, date, changes, message, llm_openai, llm_ubicloud):
    INSERT_COMMIT = """
        INSERT INTO commits ("repo", "id", "author", "date", "changes", "message", "llm_openai", "llm_ubicloud")
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT ("repo", "id") DO NOTHING;
    """
    cur.execute(INSERT_COMMIT, (repo_name, commit_id, author, date,
                changes, message, llm_openai.strip(), llm_ubicloud.strip()))
    conn.commit()


def chunk_file(file_content, context_window):
    """
    Splits the file content into chunks, ensuring that each chunk ends at a function boundary.
    Specifically, it looks for `}` at the beginning of a line as a natural break point.
    """
    chunks = []
    current_chunk = []
    current_size = 0

    # Split the content into lines for easier processing
    lines = file_content.splitlines()

    for line in lines:
        if len(line) > context_window:
            line = line[:(context_window)]
        current_chunk.append(line)
        current_size += len(line)

        # If we've reached a size limit
        if (
            (current_size >= context_window and (
                re.match(r'^\}', line) or re.match(r'^\};', line) or re.match(r'^\];$', line)))
            or (current_size >= 2 * context_window and (re.match(r'^\s{2}\}', line)))
            or (current_size >= 3 * context_window)
        ):
            chunks.append("\n".join(current_chunk))
            current_chunk = []
            current_size = 0

    # Add the remaining chunk if any content is left
    if current_chunk:
        chunks.append("\n".join(current_chunk))

    return chunks


def process_file(file_path, folder_name, repo_name):
    file_name = os.path.basename(file_path)

    # If file already has a summary, skip processing and just return it
    cur.execute(
        """SELECT "llm_openai", "llm_ubicloud" FROM files WHERE "name" = %s AND "folder" = %s AND "repo" = %s""", (file_name, folder_name, repo_name))
    row = cur.fetchone()
    if row:
        return row

    # Summarize each chunk and combine summaries
    def get_description(chunks, ask):
        if len(chunks) == 1:
            return ask(FILE_PROMPT + "\n\nFile: " + file_name + "\n\n" + chunks[0])
        else:
            descriptions = []
            for chunk in chunks:
                descriptions.append(
                    ask(FILE_PROMPT + "\n\nFile: " + file_name + "\n\n" + chunk))
            return ask(FILE_SUMMARIES_PROMPT + "\n\nFile: " + file_name + "\n\n" + "\n".join(descriptions[:10]))

    print("File:", file_path)
    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
        file_content = f.read()

        chunks_openai = chunk_file(file_content, OPENAI_CONTEXT_WINDOW)
        chunks_ubicloud = chunk_file(file_content, UBICLOUD_CONTEXT_WINDOW)

        llm_openai = get_description(chunks_openai, ask_openai)
        llm_ubicloud = get_description(chunks_ubicloud, ask_ubicloud)

        # Insert the file and its components into the database
        insert_file(file_name, folder_name, repo_name,
                    file_content, llm_openai, llm_ubicloud)

        return llm_openai, llm_ubicloud


def process_folder(folder_path, repo_path, repo_name):
    if not is_acceptable_folder(folder_path):
        return
    print("Folder:", folder_path)

    # If folder already has a summary, skip processing and just return
    cur.execute(
        """SELECT "llm_openai", "llm_ubicloud" FROM folders WHERE "name" = %s AND "repo" = %s""", (folder_path, repo_name))
    row = cur.fetchone()
    if row:
        return

    # Full relative folder path
    folder_name = os.path.relpath(folder_path, repo_path)
    llm_openai_list = []
    llm_ubicloud_list = []

    # Process each file in the folder
    for item in os.listdir(folder_path):
        item_path = os.path.join(folder_path, item)
        if os.path.isfile(item_path) and is_acceptable_file(item):
            llm_openai, llm_ubicloud = process_file(
                item_path, folder_name, repo_name)
            if llm_openai:
                llm_openai_list.append(llm_openai)
            if llm_ubicloud:
                llm_ubicloud_list.append(llm_ubicloud)
        elif os.path.isdir(item_path) and is_acceptable_folder(item_path):
            # Retrieve the summary of the subfolder from the database
            subfolder_name = os.path.relpath(item_path, repo_path)
            cur.execute(
                """SELECT "llm_openai", "llm_ubicloud" FROM folders WHERE "name" = %s AND "repo" = %s""", (subfolder_name, repo_name))
            subfolder_row = cur.fetchone()
            if subfolder_row and subfolder_row[0]:
                llm_openai = subfolder_row[0][0]
                llm_ubicloud = subfolder_row[0][1]
                if llm_openai:
                    llm_openai_list.append(llm_openai)
                if llm_ubicloud:
                    llm_ubicloud_list.append(llm_ubicloud)

    def get_description(descriptions, ask, context_window):
        max_descriptions = int(context_window / 400)
        if len(descriptions) < max_descriptions:
            return ask(FOLDER_PROMPT + "\n\n" + "\n".join(descriptions))
        else:
            combined_descriptions = []
            for i in range(0, min(len(descriptions),  max_descriptions * max_descriptions), max_descriptions):
                combined_description = ask(
                    FOLDER_PROMPT + "\n\n" + "\n".join(descriptions[i:i+max_descriptions]))
                combined_descriptions.append(combined_description)
            return ask(FOLDER_SUMMARIES_PROMPT + "\n\n" + "\n".join(combined_descriptions))

    llm_openai = get_description(
        llm_openai_list, ask_openai, OPENAI_CONTEXT_WINDOW)
    llm_ubicloud = get_description(
        llm_ubicloud_list, ask_ubicloud, UBICLOUD_CONTEXT_WINDOW)

    insert_folder(folder_name, repo_name, llm_openai, llm_ubicloud)


def extract_files_changed(diff_content):
    """
    Extracts a list of files changed from the diff content.
    """
    files_changed = set()
    for line in diff_content.splitlines():
        if line.startswith('diff --git'):
            # Example line: diff --git a/file1.txt b/file1.txt
            parts = line.split()
            if len(parts) >= 3:
                # Extract the file path (removing the 'a/' or 'b/' prefix)
                file_path = parts[2].replace('b/', '').replace('a/', '')
                files_changed.add(file_path)
    return list(files_changed)


def process_commits(repo_path, repo_name):
    # Extract commit data using git log
    os.system(
        f"git -C {repo_path} log -p -n 10000 --pretty=format:'COMMIT_HASH:%H|AUTHOR_NAME:%an|AUTHOR_EMAIL:%ae|DATE:%ad|TITLE:%s|MESSAGE:%b' --date=iso > commit_data.txt"
    )

    # Read commit data from file
    with open('commit_data.txt', 'r') as file:
        lines = file.readlines()

    # Variables to store commit data
    commit_id = author_name = author_email = commit_date = title = message = ""
    changes = ""
    in_diff_section = False

    def maybe_save_commit():
        if commit_id:
            author = f"{author_name} <{author_email}>"
            try:
                input = f"{title}\n{message}\nChanges: {changes}\nAuthor: {author}>\nDate: {commit_date}"
                llm_openai = ask_openai(COMMIT_PROMPT + "\n\n" + input)
                llm_ubicloud = ask_ubicloud(COMMIT_PROMPT + "\n\n" + input)
                insert_commit(repo_name, commit_id, author, commit_date,
                              changes, message, llm_openai, llm_ubicloud)
            except Exception as e:
                print(
                    f"Error processing commit {commit_id}: {e}")
                files_changed = extract_files_changed(changes)
                input = f"{title}\n{message}\Files changed: {', '.join(files_changed)}\nAuthor: {author}>\nDate: {commit_date}"
                llm_openai = ask_openai(COMMIT_PROMPT + "\n\n" + input)
                llm_ubicloud = ask_ubicloud(COMMIT_PROMPT + "\n\n" + input)
                insert_commit(repo_name, commit_id, author, commit_date,
                              changes, message, llm_openai, llm_ubicloud)

    # Process each line to extract and insert commit data
    for line in lines:
        line = line.strip()

        if line.startswith("COMMIT_HASH:"):
            maybe_save_commit()

            # Start reading a new commit
            commit_id = line.split("COMMIT_HASH:")[1].strip()
            in_diff_section = False
            changes = ""  # Reset the diff for the next commit

        elif line.startswith("AUTHOR_NAME:"):
            author_name = line.split("AUTHOR_NAME:")[1].strip()
        elif line.startswith("AUTHOR_EMAIL:"):
            author_email = line.split("AUTHOR_EMAIL:")[1].strip()
        elif line.startswith("DATE:"):
            commit_date = line.split("DATE:")[1].strip()
        elif line.startswith("TITLE:"):
            title = line.split("TITLE:")[1].strip()
        elif line.startswith("MESSAGE:"):
            message = line.split("MESSAGE:")[1].strip()
        elif line == "" and commit_id:
            # Empty line indicates the start of the diff section
            in_diff_section = True
        elif in_diff_section:
            # Accumulate the diff (changes)
            changes += line + "\n"

    # Insert the last commit's data
    maybe_save_commit()

    # Delete the temporary commit data file
    os.remove('commit_data.txt')


def main(repo_name):
    cur.execute(
        """SELECT "name" FROM repos WHERE "name" = %s""", (repo_name,))
    row = cur.fetchone()
    if row:
        print(f"Repository '{repo_name}' already processed. Exiting...")
        return

    # Validate the correct repo path
    repo_path = f"repos/{repo_name}"
    if not os.path.exists(repo_path):
        print(
            f"Repository '{repo_name}' not found at expected path {repo_path}. Exiting...")
        return

    # Process commits
    process_commits(repo_path, repo_name)

    # Walk through the directory tree
    for root, dirs, files in os.walk(repo_path, topdown=False):
        dirs[:] = [d for d in dirs if is_acceptable_folder(d)]
        process_folder(root, repo_path, repo_name)
    insert_repo(repo_name)

    backfill(repo_name)


if __name__ == '__main__':
    if len(sys.argv) == 2:
        main(sys.argv[1])
        cur.close()
        conn.close()
    else:
        print("Usage: python process_repo.py <repo>")
