from collections import Counter
from datetime import datetime
import json
import frontmatter
import glob
import os
import requests
import random
import markdown
from markdown.extensions.codehilite import CodeHiliteExtension
from markdown.extensions.sane_lists import SaneListExtension
from markdown.extensions.nl2br import Nl2BrExtension
from markdown.extensions.fenced_code import FencedCodeExtension
from markdown.extensions.footnotes import FootnoteExtension
from markdown.extensions.tables import TableExtension
import en_core_web_sm
from word_freq_hist import get_histogram_of_words, visualize_histogram

nlp = en_core_web_sm.load()


def check_if_word_is_name(word):
    global nlp
    doc = nlp(word)
    return any(ent.pos_ == "NOUN" for ent in doc)


def convert_markdown_to_html(markdown_content):
    extensions = [
        CodeHiliteExtension(),
        SaneListExtension(),
        Nl2BrExtension(),
        FencedCodeExtension(),
        FootnoteExtension(),
        TableExtension(),
    ]
    html_content = markdown.markdown(markdown_content, extensions=extensions)
    return html_content


def should_mask_word(
    word, inside_code_block, inside_inline_code, inside_hyperlink, processes_metadata
):
    if (
        inside_code_block
        or inside_inline_code
        or inside_hyperlink
        or word.startswith("```")
        or processes_metadata
    ):
        return False
    if len(word) <= 2 or not any(c.isalnum() for c in word):
        return False
    if not check_if_word_is_name(word):
        return False
    return random.random() < 0.3


def mask_content(content):
    lines = content.split("\n")
    masked_lines = []
    processes_metadata = True
    metadatas = {
        "Title": "",
        "Status": "",
        # Format is inconsistent in the original data
        "Authors": "",
        "Author": "",
        "Review Manager": "",
    }
    inside_code_block = False

    for line in lines:
        if line.startswith("# "):
            metadatas["Title"] = line[2:].strip()
            masked_lines.append(line)
            continue

        if line.startswith("##"):
            processes_metadata = False

        if line.startswith("#") or line.startswith("---") or not line.strip():
            masked_lines.append(line)
            continue

        if processes_metadata:
            if any(line[2:].startswith(key) for key in metadatas.keys()):
                key = line[2:].split(":")[0]
                prefix = len(key) + 2 + 1
                if prefix < len(line):
                    metadatas[key] = line[prefix:].strip()
                masked_lines.append(line)
                continue

        if line.startswith("```"):
            inside_code_block = not inside_code_block

        if inside_code_block:
            masked_lines.append(line)
            continue

        words: list[str] = line.split()
        inside_inline_code = [False, False]
        inside_hyperlink = [False, False, False, False]
        masked_words: list[str] = []

        for word in words:

            if all(inside_hyperlink):
                inside_hyperlink = [False, False, False, False]

            if all(inside_inline_code):
                inside_inline_code = [False, False]

            if "`" in word:
                # If inline code is not closed, we should not mask the word.
                if inside_inline_code[0]:
                    inside_inline_code[1] = True
                else:
                    inside_inline_code[0] = True

            if "[" in word:
                inside_hyperlink[0] = True
            if inside_hyperlink[0] and "]" in word:
                inside_hyperlink[1] = True
            if inside_hyperlink[1] and "(" in word:
                inside_hyperlink[2] = True
            if inside_hyperlink[2] and ")" in word:
                inside_hyperlink[3] = True

            is_inside_hyperlink = any(inside_hyperlink)
            is_inside_inline_code = any(inside_inline_code)

            if should_mask_word(
                word=word,
                inside_code_block=inside_code_block,
                inside_inline_code=is_inside_inline_code,
                inside_hyperlink=is_inside_hyperlink,
                processes_metadata=processes_metadata,
            ):
                contains_punctuation = word[-1] in [
                    ".",
                    ",",
                    "!",
                    "?",
                    ":",
                    ";",
                    "-",
                    "_",
                    "~",
                    "|",
                    "=",
                    "+",
                    "*",
                    "/",
                    "\\",
                    "@",
                ]
                if contains_punctuation:
                    masked_word_and_punctuation = r"◻︎" * (len(word) - 1) + word[-1]
                    masked_words.append(masked_word_and_punctuation)
                else:
                    masked_words.append(r"◻︎" * len(word))
            else:
                masked_words.append(word)

        masked_line = " ".join(masked_words)

        if line.endswith((" ", "\t")):
            masked_line += line[len(line.rstrip()) :]

        masked_lines.append(masked_line)

    return "\n".join(masked_lines), metadatas


def upload_to_microcms(proposal_data):
    api_key = os.environ["MICROCMS_API_KEY"]
    domain = os.environ["MICROCMS_SERVICE_DOMAIN"]
    endpoint = f"https://{domain}.microcms.io/api/v1/proposals"

    headers = {"X-MICROCMS-API-KEY": api_key, "Content-Type": "application/json"}

    html_content = convert_markdown_to_html(proposal_data["content"])

    microcms_data = {
        "title": proposal_data["title"],
        "content": html_content,
        "proposalId": proposal_data["proposal_id"],
        "status": proposal_data["status"],
        "authors": proposal_data["authors"],
        "reviewManager": proposal_data.get("review_manager", ""),
    }

    response = requests.post(endpoint, headers=headers, json=microcms_data)
    response.raise_for_status()
    return response.json()


all_proposals = []


def delete_proposal(proposal_id: str):
    print(f"Deleting proposal {proposal_id} from microcms...")
    api_key = os.environ["MICROCMS_API_KEY"]
    domain = os.environ["MICROCMS_SERVICE_DOMAIN"]
    endpoint = f"https://{domain}.microcms.io/api/v1/proposals"

    headers = {"X-MICROCMS-API-KEY": api_key, "Content-Type": "application/json"}
    try:
        content_id = list(
            filter(
                lambda proposal: proposal["proposalId"] == proposal_id, all_proposals
            )
        )[0]["id"]
        delete_endpoint = f"{endpoint}/{content_id}"
        response = requests.delete(delete_endpoint, headers=headers)
        response.raise_for_status()
        print(f"Successfully deleted proposal {proposal_id} from microcms")
    except Exception as e:
        # Not raise an error in case the proposal does not exist.
        print(f"Error deleting proposal {proposal_id} from microcms: {str(e)}")


def preprocess_microcms_data():
    print("Fetching all proposals from microcms...")
    # Delete all proposals in microcms
    api_key = os.environ["MICROCMS_API_KEY"]
    domain = os.environ["MICROCMS_SERVICE_DOMAIN"]
    endpoint = f"https://{domain}.microcms.io/api/v1/proposals"

    headers = {"X-MICROCMS-API-KEY": api_key, "Content-Type": "application/json"}
    # First, get all proposals. Iterate over all pages (around 500 contents in total).
    for _ in range(5):
        response = requests.get(f"{endpoint}?limit=100", headers=headers)
        response.raise_for_status()
        proposals = response.json()["contents"]
        all_proposals.extend(proposals)
    print("Done fetching all proposals from microcms")


def main():
    random.seed(42)
    preprocess_microcms_data()
    proposal_files = sorted(list(glob.glob("proposals/*.md")))

    all_word_freq_hists = Counter()

    for file_path in proposal_files:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                post = frontmatter.loads(f.read())

            proposal_id = os.path.basename(file_path).split("-")[0]
            masked_content, metadatas = mask_content(post.content)
            word_freq_hist = get_histogram_of_words(nlp, post.content)
            all_word_freq_hists += word_freq_hist

            proposal_data = {
                "title": metadatas["Title"],
                "content": masked_content,
                "proposal_id": proposal_id,
                "status": metadatas["Status"],
                "authors": metadatas["Authors"] or metadatas["Author"],
                "review_manager": metadatas["Review Manager"],
            }
            # Before uploading, delete the proposal from microcms if it exists.
            delete_proposal(proposal_id)
            result = upload_to_microcms(proposal_data)
            print(f"Successfully uploaded proposal {proposal_id}")

        except Exception as e:
            print(f"Error processing {file_path}: {str(e)}")

    visualize_histogram(all_word_freq_hists, write_to_file=True)


if __name__ == "__main__":
    main()
