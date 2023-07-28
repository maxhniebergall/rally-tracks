import asyncio
import base64
import itertools
import json
import os

from elasticsearch import BadRequestError, NotFoundError


class WeightedTermsParamsSource:
    def __init__(self, track, params, **kwargs):
        # choose a suitable index: if there is only one defined for this track
        # choose that one, but let the user always override index
        if len(track.indices) == 1:
            default_index = track.indices[0].name
        else:
            default_index = "_all"

        self._index_name = params.get("index", default_index)
        self._cache = params.get("cache", False)
        self._size = params.get("size", 10)
        self._field = params.get("field", "ml.tokens")
        self._tokens_file = params.get("tokens-source", "elser-query-tokens.json")
        self._num_terms = params.get("num-terms", 10)
        self._track_total_hits = params.get("track_total_hits", False)
        self._params = params

        cwd = os.path.dirname(__file__)
        with open(os.path.join(cwd, self._tokens_file), "r") as file:
            self._query_tokens = json.load(file)

        self._iters = 0

    def partition(self, partition_index, total_partitions):
        return self

    def params(self):
        query = self._query_tokens[self._iters]
        if self._num_terms > len(query):
            raise Exception(f"The requested number of terms {self._num_terms} cannot be satisfied by the query with {len(query)} tokens")

        result = {"index": self._index_name, "cache": self._cache, "size": self._size}
        result["body"] = {
            "query": {
                "bool": {
                    "should": [
                        {"term": {f"{self._field}": {"value": f"{key}", "boost": value}}}
                        for key, value in itertools.islice(query.items(), self._num_terms)
                    ]
                }
            },
            "track_total_hits": self._track_total_hits,
        }
        self._iters = (self._iters + 1) % len(self._query_tokens)

        return result


elser_model_id = ".elser_model_1"


async def put_elser(es, params):
    try:
        await es.ml.put_trained_model(model_id=elser_model_id, input={"field_names": "text_field"})
        return True
    except BadRequestError as bre:
        if (
            bre.body["error"]["root_cause"][0]["reason"]
            == "Cannot create model [.elser_model_1] the id is the same as an current model deployment"
            or bre.body["error"]["root_cause"][0]["reason"] == "Trained machine learning model [.elser_model_1] already exists"
        ):
            return True
        else:
            print(bre)
            return False
    except Exception as e:
        print(e)
        return False


async def poll_for_elser_completion(es, params):
    try_count = 0
    max_wait_time_seconds = 120
    wait_time_per_cycle_seconds = 5
    while wait_time_per_cycle_seconds * try_count < max_wait_time_seconds:
        try:
            response = await es.ml.get_trained_models(model_id=elser_model_id, include="definition_status")
            if is_model_fully_defined(response):
                return True
        except NotFoundError:
            print("\nwaiting... try count:", try_count, end="")
            await asyncio.sleep(wait_time_per_cycle_seconds)
            try_count += 1
    print()
    return False


def is_model_fully_defined(response):
    return response["trained_model_configs"][0]["fully_defined"]


async def stop_trained_model_deployment(es, params):
    number_of_allocations = params["number_of_allocations"]
    threads_per_allocation = params["threads_per_allocation"]

    try:
        print("stop_trained_model_deployment:")
        await es.ml.stop_trained_model_deployment(model_id=elser_model_id, force=True)
        return True
    except BadRequestError as bre:
        if model_deployment_already_exists(bre):
            return True
        else:
            print(bre)
            return False


async def start_trained_model_deployment(es, params):
    number_of_allocations = params["number_of_allocations"]
    threads_per_allocation = params["threads_per_allocation"]
    queue_capacity = params["queue_capacity"]
    try:
        await es.ml.start_trained_model_deployment(
            model_id=elser_model_id,
            wait_for="fully_allocated",
            number_of_allocations=number_of_allocations,
            threads_per_allocation=threads_per_allocation,
        )
        return True
    except BadRequestError as bre:
        if model_deployment_already_exists(bre):
            return True
        else:
            print(bre)
            return False
    except Exception as e:
        print("Exception", e)
        return False


def model_deployment_already_exists(badRequestError):
    exists = (
        badRequestError.body["error"]["root_cause"][0]["reason"]
        == "Could not start model deployment because an existing deployment with the same id [.elser_model_1] exist"
    )

    return exists


async def create_elser_model(es, params):
    if await put_elser(es, params) == False:
        return False
    if await poll_for_elser_completion(es, params) == False:
        return False
    await stop_trained_model_deployment(es, params)
    await start_trained_model_deployment(es, params)


def register(registry):
    registry.register_param_source("weighted-terms-param-source", WeightedTermsParamsSource)
    registry.register_runner("create-elser-model", create_elser_model, async_runner=True)
