"""Text processing utilities for PR description formatting."""
import re


def sanitize_diagram(diagram_raw: str) -> str:
    """Sanitize a diagram string: fix missing closing fence and remove backticks."""
    if not isinstance(diagram_raw, str):
        return ''
    diagram = diagram_raw.strip()
    if not diagram.startswith('```mermaid'):
        return ''

    # fallback missing closing
    if not diagram.endswith('```'):
        diagram += '\n```'

    # remove backticks inside node labels: ["`label`"] -> ["label"]
    result = []
    for line in diagram.split('\n'):
        line = re.sub(
            r'\["([^"]*?)"\]',
            lambda m: '["' + m.group(1).replace('`', '') + '"]',
            line,
        )
        result.append(line)
    return '\n' + '\n'.join(result)


def count_chars_without_html(string):
    """Count characters in a string, excluding HTML tags."""
    if '<' not in string:
        return len(string)
    no_html_string = re.sub('<[^>]+>', '', string)
    return len(no_html_string)


def replace_code_tags(text):
    """
    Replace odd instances of ` with <code> and even instances of ` with </code>
    """
    parts = text.split('`')
    for i in range(1, len(parts), 2):
        parts[i] = '<code>' + parts[i] + '</code>'
    return ''.join(parts)


def insert_br_after_x_chars(text: str, x=70):
    """
    Insert <br> into a string after a word that increases its length above x characters.
    Use proper HTML tags for code and new lines.
    """

    if not text:
        return ""
    if count_chars_without_html(text) < x:
        return text

    is_list = text.lstrip().startswith(("- ", "* "))

    # replace odd instances of ` with <code> and even instances of ` with </code>
    text = replace_code_tags(text)

    # convert list items to <li> only if the text is identified as a list
    if is_list:
        # To handle lists that start with indentation
        leading_whitespace = text[:len(text) - len(text.lstrip())]
        body = text.lstrip()
        body = "<li>" + body[2:]
        text = leading_whitespace + body

        text = text.replace("\n- ", '<br><li> ').replace("\n - ", '<br><li> ')
        text = text.replace("\n* ", '<br><li> ').replace("\n * ", '<br><li> ')

    # convert new lines to <br>
    text = text.replace("\n", '<br>')

    # split text into lines
    lines = text.split('<br>')
    words = []
    for i, line in enumerate(lines):
        words += line.split(' ')
        if i < len(lines) - 1:
            words[-1] += "<br>"

    new_text = []
    is_inside_code = False
    current_length = 0
    for word in words:
        is_saved_word = False
        if word == "<code>" or word == "</code>" or word == "<li>" or word == "<br>":
            is_saved_word = True

        len_word = count_chars_without_html(word)
        if not is_saved_word and (current_length + len_word > x):
            if is_inside_code:
                new_text.append("</code><br><code>")
            else:
                new_text.append("<br>")
            current_length = 0  # Reset counter
        new_text.append(word + " ")

        if not is_saved_word:
            current_length += len_word + 1  # Add 1 for the space

        if word == "<li>" or word == "<br>":
            current_length = 0

        if "<code>" in word:
            is_inside_code = True
        if "</code>" in word:
            is_inside_code = False

    processed_text = ''.join(new_text).strip()

    if is_list:
        processed_text = f"<ul>{processed_text}</ul>"

    return processed_text
