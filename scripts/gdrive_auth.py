"""Run a one-time local OAuth consent flow and print a Drive refresh token."""

from __future__ import annotations

import argparse
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from app.core.filestore import DRIVE_SCOPE


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("client_secrets", type=Path, help="Downloaded Desktop OAuth client JSON")
    parser.add_argument(
        "--create-root-folder",
        action="store_true",
        help="Create the app-accessible Drive root folder and print its ID",
    )
    args = parser.parse_args()
    flow = InstalledAppFlow.from_client_secrets_file(str(args.client_secrets), scopes=[DRIVE_SCOPE])
    credentials = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    if not credentials.refresh_token:
        raise RuntimeError("Google did not return a refresh token. Revoke the app's access and run consent again.")
    print(credentials.refresh_token)
    if args.create_root_folder:
        drive = build("drive", "v3", credentials=credentials, cache_discovery=False)
        folder = (
            drive.files()
            .create(
                body={"name": "TrustUsHealth Reports", "mimeType": "application/vnd.google-apps.folder"},
                fields="id",
            )
            .execute()
        )
        print(f"GDRIVE_ROOT_FOLDER_ID={folder['id']}")


if __name__ == "__main__":
    main()
