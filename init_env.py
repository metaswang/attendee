import os
from pathlib import Path


def main():
    output_path = Path(".env.generated")
    output_path.write_text(
        "\n".join(
            [
                "CREDENTIALS_ENCRYPTION_KEY=",
                "DJANGO_SECRET_KEY=",
                "AWS_RECORDING_STORAGE_BUCKET_NAME=",
                "AWS_ACCESS_KEY_ID=",
                "AWS_SECRET_ACCESS_KEY=",
                "AWS_DEFAULT_REGION=us-east-1",
                "",
            ]
        ),
        encoding="utf-8",
    )
    os.chmod(output_path, 0o600)
    print(f"Wrote generated environment variables to {output_path}")


if __name__ == "__main__":
    main()
