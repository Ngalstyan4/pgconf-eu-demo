import os
import sys
import psycopg2
from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")
MODEL = "gpt-4o-mini"

conn = psycopg2.connect(DATABASE_URL)
cur = conn.cursor()


repo = sys.argv[1]
print("repo:", repo)

# Select folders where zero slash or one slash is present
cur.execute(
    """SELECT "name", "description" FROM folders WHERE "name" NOT LIKE '%%/%%/%%' AND "name" <> '.' AND "repo" = %s AND "model" = %s;""", (repo, MODEL))
rows = cur.fetchall()

for row in rows:
    print('--------------------------')
    print("Folder:", row[0])
    print('----------')
    print(row[1])
    print('--------------------------\n')

if len(rows) == 0:
    cur.execute(
        """SELECT "name", "description", "folder" FROM files WHERE "repo" = %s AND "model" = %s;""", (repo, MODEL))
    rows = cur.fetchall()

    for row in rows:
        print('--------------------------')
        print("Folder:", row[2], "File:", row[0])
        print('----------')
        print(row[1])
        print('--------------------------\n')