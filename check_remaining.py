import os
import sheets


def main():
    remaining = sheets.count_unprocessed_rows()

    print(f"📌 Remaining unprocessed rows: {remaining}")

    github_output = os.environ.get("GITHUB_OUTPUT")

    if github_output:
        with open(github_output, "a", encoding="utf-8") as f:
            f.write(f"remaining={remaining}\n")

    if remaining > 0:
        sheets.add_log(
            row_number="",
            status="REMAINING_ROWS_FOUND",
            log_type="CHECKER",
            message=f"{remaining} rows still unprocessed"
        )
    else:
        sheets.add_log(
            row_number="",
            status="ALL_DONE",
            log_type="CHECKER",
            message="All rows processed"
        )


if __name__ == "__main__":
    main()
