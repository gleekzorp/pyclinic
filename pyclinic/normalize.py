import re


def normalize_function_name(name: str) -> str:
    """Turn a name into a pythonic function name.

    Example:
        Draw Cards => draw_cards
    """
    string = re.sub(r"[?!@#$%^&*()_\-+=,./\'\\\"|:;{}\[\]]", " ", name)
    return "_".join(string.lower().split())


def normalize_class_name(name: str) -> str:
    """Turn a name into a pythonic class name.

    Example:
        Folder 1 => Folder1
    """
    string = re.sub(r"[?!@#$%^&*()_\-+=,./\'\\\"|:;{}\[\]]", " ", name)
    words = []
    for word in string.split():
        word = word[0].upper() + word[1:]
        words.append(word)

    return "".join(words)
