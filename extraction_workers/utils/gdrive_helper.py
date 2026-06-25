import os
import json
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    HAS_GDRIVE_LIBS = True
except ImportError:
    HAS_GDRIVE_LIBS = False
    
    
def load_gdrive_credentials(extraction_params):
    if not HAS_GDRIVE_LIBS:
        raise ImportError(
            "Google client libraries (google-api-python-client, google-auth) are not installed."
        )

    scopes = ["https://www.googleapis.com/auth/drive"]

    creds_param = extraction_params.get("gdrive_credentials")
    if creds_param:
        if isinstance(creds_param, dict):
            logger.info(
                "Loading Google Drive credentials from dictionary in extraction_params."
            )
            return service_account.Credentials.from_service_account_info(
                creds_param, scopes=scopes
            )
        elif isinstance(creds_param, str):
            try:
                info = json.loads(creds_param)
                logger.info(
                    "Loading Google Drive credentials from JSON string in extraction_params."
                )
                return service_account.Credentials.from_service_account_info(
                    info, scopes=scopes
                )
            except json.JSONDecodeError:
                if os.path.exists(creds_param):
                    logger.info(
                        f"Loading Google Drive credentials from file path in extraction_params: {creds_param}"
                    )
                    return service_account.Credentials.from_service_account_file(
                        creds_param, scopes=scopes
                    )
                else:
                    logger.warning(
                        f"Credentials path in extraction_params does not exist: {creds_param}"
                    )

    env_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if env_path and os.path.exists(env_path):
        logger.info(
            f"Loading Google Drive credentials from GOOGLE_APPLICATION_CREDENTIALS: {env_path}"
        )
        return service_account.Credentials.from_service_account_file(
            env_path, scopes=scopes
        )

    search_paths = ["/app/data/gdrive_credentials.json"]
    for path in search_paths:
        if os.path.exists(path):
            logger.info(f"Loading Google Drive credentials from candidate path: {path}")
            return service_account.Credentials.from_service_account_file(
                path, scopes=scopes
            )

    raise FileNotFoundError("Google Drive credentials not found.")


def list_gdrive_files(service, folder_id):
    files = set()
    page_token = None
    while True:
        try:
            query = f"'{folder_id}' in parents and trashed = false"
            response = (
                service.files()
                .list(
                    q=query,
                    spaces="drive",
                    fields="nextPageToken, files(id, name)",
                    pageToken=page_token,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )
            for f in response.get("files", []):
                files.add(f["name"])
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        except Exception as e:
            logger.error(f"Error listing Google Drive files: {e}")
            raise
    return files


def upload_file_to_gdrive(service, file_path, folder_id):
    file_name = os.path.basename(file_path)
    logger.info(f"Uploading {file_name} to Google Drive...")

    if file_path.endswith(".pdf"):
        mime_type = "application/pdf"
    elif file_path.endswith(".json"):
        mime_type = "application/json"
    elif file_path.endswith(".html"):
        mime_type = "text/html"
    else:
        mime_type = "application/octet-stream"

    file_metadata = {"name": file_name, "parents": [folder_id]}
    media = MediaFileUpload(file_path, mimetype=mime_type, resumable=True)

    try:
        file_obj = (
            service.files()
            .create(
                body=file_metadata,
                media_body=media,
                fields="id",
                supportsAllDrives=True,
            )
            .execute()
        )
        logger.info(
            f"Successfully uploaded {file_name} to Google Drive (ID: {file_obj.get('id')})"
        )
        return True
    except Exception as e:
        logger.error(f"Error uploading {file_name} to Google Drive: {e}")
        return False