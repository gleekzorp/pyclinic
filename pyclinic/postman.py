import json
import logging
import os
import re
from typing import Dict, List

import requests
from jsonpath_ng import parse
from jsonpath_ng.jsonpath import DatumInContext, Fields, Index, Root

from pyclinic import model_parser
from pyclinic.normalize import normalize_class_name, normalize_function_name
from pyclinic.models.postman_collection_model import PostmanCollection, PostmanRequest

_logger = logging.getLogger(__name__)


def load_postman_collection_from_str(json_str: str) -> PostmanCollection:
    collection = json.loads(json_str)
    return PostmanCollection(**collection)


def load_postman_collection_from_url(collection_url: str) -> PostmanCollection:
    _logger.debug(f"Requesting Postman Collection from URL: {collection_url}")
    response = requests.get(collection_url)
    if not response.ok:
        raise ValueError(f"Unable to get postman collection from url: {collection_url}")

    return PostmanCollection(**response.json())


def load_postman_collection_from_file(collection_file_path: str) -> PostmanCollection:
    _logger.debug(f"Looking for Postman Collection from file path: {collection_file_path}")
    try:
        with open(collection_file_path, "r") as f:
            collection = load_postman_collection_from_str(f.read())
    except FileNotFoundError:
        raise ValueError(f"Unable to find or open file: {collection_file_path}")
    return collection


def load_variables(variables_path: str) -> Dict:
    try:
        with open(variables_path, "r") as f:
            variables = json.load(f)
    except Exception as e:
        _logger.fatal(f"Unable to load Postman Variables from file: {variables_path}")
        raise e
    return variables


def flatten_variables(postman_variables: List[Dict]) -> Dict[str, Dict]:
    """Convert a list of postman variable objects to a flat dictionary."""
    values = postman_variables.copy()
    variables = {}

    for value in values:
        # value => {'enabled': True, 'key': 'BASE_URL', 'value': 'https://demoqa.com'}
        name = value.pop("key")
        variables[name] = value

    return variables


def find_request_ascendants(request_context: DatumInContext) -> List[str]:
    """Find the respective folder ascendants for the given Postman Request.

    Example:
        ["Root", "Folder", "Subfolder"]
    """
    ascendants = []

    while type(request_context.path) is not Root:
        if type(request_context.path) is Index:
            request_context = request_context.context  # go up a level

        if request_context.path == Fields("item"):
            request_context = request_context.context  # go up a level

        try:
            name = request_context.value.get("name")
            ascendants.append(name)
        except:
            break
        request_context = request_context.context  # go up a level

    ascendants.append("Root")
    return ascendants[::-1]


def build_request_to_send(collection: PostmanCollection, variables: Dict, request: PostmanRequest) -> Dict:
    """Convert a single PostmanRequest to a dictionary to be used by requests."""
    # Replace variables with their actual values
    raw_url = request.url.raw
    for found_var in re.findall(pattern=r"\{([^}]+)\}", string=raw_url):
        key = found_var[1:]  # remove leading "{" not caught by regex
        var = variables.get(key)  # => {'value': 'foo', 'enabled': True}
        if var is None:
            raise ValueError(f"{raw_url} => Variable {key} not found in collection, environment, or global variables.")

        if var.get("enabled") is False:
            raise ValueError(f"{raw_url} => Variable {key} was found, but enabled was set to False")

        if var.get("value") is None or var.get("value") == "":
            _logger.warning(f"{raw_url} => Variable {key} was found, but value was set to None or empty.")
        raw_url = raw_url.replace("{{" + key + "}}", var.get("value"))

    # Build the request dictionary (method, url, headers, data, etc.)
    request_dict = request.dict()
    if request.body:
        if request.body.get("raw"):
            request_dict["data"] = json.loads(request.body.get("raw"))
        elif request.body.get("urlencoded"):
            request_dict["data"] = str(request.body.get("urlencoded"))
        else:  # TODO: handle other types of request body like GraphQL and form-data
            pass
    del request_dict["body"]  # uses ["data"] instead of ["body"]
    request_dict["url"] = raw_url

    request_dict["headers"] = {}
    header = request_dict.pop("header")
    if header:
        for item in header:
            key = item["key"]
            value = item["value"]
            request_dict["headers"].update({key: value})
    return request_dict


def map_response_bodies_to_folders(collection: PostmanCollection) -> Dict:
    matches: List[DatumInContext] = [match for match in parse("$..[*] where response").find(collection.item)]
    folders = {}
    item_names = set()

    for item in matches:
        # Item names must be unique
        item_name = item.value.get("name")
        if item_name in item_names:
            continue
        item_names.add(item_name)

        # Find respective parent folder for this item
        folder_name = find_request_ascendants(item)[-1]

        # Setup folder and file if they don't already exist
        if folder_name not in folders:
            folders[folder_name] = {}
        if item_name not in folders[folder_name]:
            folders[folder_name][item_name] = []

        # Parse response body and add to its folder.item
        for resp in item.value.get("response"):
            body = resp.get("body")
            if type(body) is str and body != "":
                body = json.loads(body)
            folders[folder_name][item_name].append(body)
    return folders


def write_model_to_file(filename: str, model: str):
    """Write or append a single Pydantic Model to the given file."""
    _logger.debug(f"Writing model to file at: {filename}")
    if os.path.exists(filename):
        with open(filename, "r") as f:
            contents = f.read()

        with open(filename, "a") as f:
            for line in model.split("\n\n"):
                if line not in contents:
                    f.write(line + "\n")
    else:
        with open(filename, "w") as f:
            f.write(model)


def write_collection_models_to_files(folders: Dict) -> List[str]:
    """Write all collection response bodies to Pydantic Model files.

    Write to ./models or ./models/clinic and return the path to the directory the models were saved to.
    """
    path = "./models"
    if os.path.exists(path):
        path += "/clinic"
        os.mkdir(path)
    else:
        os.mkdir(path)

    _logger.info(f"Writing model files to: {path} directory")
    paths = [path]

    for model_file in folders.keys():
        for value in folders[model_file].values():
            models = [model_parser.json_to_model(body) for body in list(filter(None, value))]
            model_file_name = normalize_function_name(model_file)
            filename = f"{path}/{model_file_name}_model.py"
            paths.append(filename)
            for model in models:
                write_model_to_file(filename, model)
    return paths


class PostmanExecutableRequest:
    def __init__(self, request: Dict):
        self.request = request

    def __call__(self, **kwargs):
        if kwargs:
            self.request.update(kwargs)
        response = requests.request(**self.request)
        return response


class PostmanFolder:
    def __init__(self, folder: Dict[str, PostmanExecutableRequest]):
        """A flat dictionary with request names as keys and their executables as the values."""
        self.folder = folder

    def __getattr__(self, name):
        if name not in self.folder:
            raise ValueError("Postman Folder doesn't have a request named: " + name)
        return self.folder[name]


def find_requests(collection: PostmanCollection, variables: Dict) -> Dict:
    """Find all request objects and flat map them to their respective folders.

    Returns a Dict like:
    {
        'pets': {
            'create_a_pet': {'headers': [], 'method': 'POST', 'url': 'http://petstore.swagger.io/v1/pets'},
            'info_for_a_specific_pet': {'headers': [], 'method': 'GET', 'url': 'http://petstore.swagger.io/v1/pets/:petId'},
            'list_all_pets': {'headers': [], 'method': 'GET', 'url': 'http://petstore.swagger.io/v1/pets?limit=-71686804'},
        },
    }
    """
    matches: List[DatumInContext] = [match for match in parse("$..[*] where response").find(collection.item)]
    folders = {}

    for match in matches:
        # Normalize folder name from "pets" => "Pets", or "{order id}" => "OrderId"
        folder_name = find_request_ascendants(match)[-1]
        folder_name = normalize_class_name(folder_name)
        if folder_name not in folders:
            folders[folder_name] = {}

        request_name = normalize_function_name(match.value["name"])
        request_to_send = build_request_to_send(collection, variables, PostmanRequest(**match.value["request"]))
        if request_name not in folders[folder_name]:
            folders[folder_name][request_name] = request_to_send
    return folders


def map_requests_to_executable_functions(folders: Dict) -> Dict:
    """Map requests to executable functions to be used by Postman runner.

    Returns a Dict like:
    {
        "Pets": PostmanFolder({
            "list_all_pets": PostmanExecutableRequest(request_to_send),
            "create_a_pet": PostmanExecutableRequest(request_to_send),
            "info_for_a_pet": PostmanExecutableRequest(request_to_send)
        })
    }
    """
    for folder_name in folders.keys():
        folder = folders[folder_name]
        for request_name in folder.keys():
            request = folder[request_name]
            folder[request_name] = PostmanExecutableRequest(request)
        folders[folder_name] = PostmanFolder(folder)
    return folders


class Postman:
    """Instance of a Postman Collection with executable request functions.

    Args:
        collection_path: The path to the Postman collection file (*.postman_collection.json)
        env_variables_path: The path to the Postman environment variables file (*.postman_environment.json)
        global_env_variables: The path to the Postman global environment variables file (*.postman_globals.json)

    Format:
        Postman.PostmanFolderRequest.PostmanExecutableRequest(**kwargs)

    Examples:
        runner.Pets.list_all_pets()
        runner.Accounts.create_a_user(data={"username": "johndoe", "password": "secret"})

    * You can override the Request that the endpoint function sends. Here are some common options:
        headers: A list of headers to send with the request.
        method: The HTTP method to use.
        url: The URL to send the request to.
        params: A dictionary of parameters to send with the request.
        data: The body of the request.
    """

    def __init__(self, collection_path, env_variables_path=None, global_variables_path=None):
        self.collection = load_postman_collection_from_file(collection_path)
        self.variables = self.__extract_variables(env_variables_path, global_variables_path)
        self.folders = self.__load()

    def __load(self):
        reqs = find_requests(self.collection, self.variables)
        return map_requests_to_executable_functions(reqs)

    def __extract_variables(self, env_variables_path, global_variables_path) -> Dict:
        variables = {}
        if global_variables_path:
            _globals = load_variables(global_variables_path)
            variables.update(flatten_variables(_globals["values"]))

        if env_variables_path:
            environment = load_variables(env_variables_path)
            variables.update(flatten_variables(environment["values"]))

        if self.collection.variable:
            variables.update(flatten_variables(self.collection.variable))

        return variables

    def __getattr__(self, name):
        if name not in self.folders:
            raise ValueError("Postman Collection doesn't have a folder named: " + name)
        folder = self.folders[name]
        return folder