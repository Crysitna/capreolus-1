# Getting Started

- Requirements: Python 3.6+, a Python environment you can install packages in (e.g., `virtualenv`), and Java 11. See the [detailed installation instructions](installation) for help with these.
- Install: `pip install capreolus`

```eval_rst
.. note:: Results and cached objects are stored in ``~/.capreolus/results/`` and ``~/.capreolus/cache/`` by default. Set the ``CAPREOLUS_RESULTS`` and ``CAPREOLUS_CACHE`` environment variables to change these locations. For example: ``export CAPREOLUS_CACHE=/data/capreolus/cache``
```

## Command Line Interface

Use the `RankTask` pipeline to rank documents using a `Searcher` on an [Anserini](https://anserini.io) `Index` built on robust04. (The index will be automatically downloaded if `benchmark.collection.path` is invalid.)
```
$ capreolus rank.searcheval with searcher.name=BM25 \
  searcher.index.stemmer=porter searcher.b=0.8 \
  benchmark.name=robust04.yang19 benchmark.collection.path=/path/to/trec45
```

## Python API

Let's run the same pipeline using the Python API:
```python
from capreolus.task.rank import RankTask

task = RankTask({'searcher': {'name': 'BM25', 'index': {'stemmer': 'porter'}, 'b': '0.8'},
                 'benchmark': {'name': 'robust04.yang19',
                               'collection': {'path': '/path/to/trec45'}}})
task.searcheval()
```

```eval_rst
.. note:: The ``capreolus.parse_config_string`` convenience method can transform a config string like ``searcher.name=BM25 benchmark.name=robust04.yang`` into a config dict as shown above.
```

<img style="float: right" src="_static/ranktask.png">


<p style="text-align: justify">
Capreolus pipelines are composed of self-contained modules corresponding to "IR primitives", which can also be used individually. Each module declares any module dependencies it needs to perform its function. The pipeline itself, which can be viewed as a dependency graph, is represented by a <code class="docutils literal notranslate"><span class="pre">Task</span></code> module.
</p>

<p style="text-align: justify">
<code class="docutils literal notranslate"><span class="pre">RankTask</span></code> declares dependencies on a <code class="docutils literal notranslate"><span class="pre">Searcher</span></code> module and a <code class="docutils literal notranslate"><span class="pre">Benchmark</span></code> module, which it uses to query a document collection and to obtain experimental data (i.e., topics, relevance judgments, and folds), respectively. The <code class="docutils literal notranslate"><span class="pre">Searcher</span></code> depends on an <code class="docutils literal notranslate"><span class="pre">Index</span></code>. Both the <code class="docutils literal notranslate"><span class="pre">Index</span></code> and <code class="docutils literal notranslate"><span class="pre">Benchmark</span></code> depend on a <code class="docutils literal notranslate"><span class="pre">Collection</span></code>. In this example, <code class="docutils literal notranslate"><span class="pre">RankTask</span></code> requires that the same <code class="docutils literal notranslate"><span class="pre">Collection</span></code> be provided to both.
</p>

Let's construct this graph one module at a time.
```python
# Previously, the Benchmark specified a dependency on the 'robust04' collection specifically.
# Now we specify "robust04" ourselves.
>>> collection = Collection.create("robust04", config={'path': '/path/to/trec45'})
>>> collection.get_path_and_types()
    ("/path/to/trec45", "TrecCollection", "DefaultLuceneDocumentGenerator")
# Next, create a Benchmark and pass it the collection object directly.
# This is an alternative to automatically creating the collection as a dependency.
>>> benchmark = Benchmark.create("robust04.yang19", provide={'collection': collection})
>>> benchmark.topics
    {'title': {'301': 'International Organized Crime', '302': 'Poliomyelitis and Post-Polio', ... }
```

Next, we can build `Index` and `Searcher`. These module types do more than just pointing to data.
```python
>>> index = Index.create("anserini", {"stemmer": "porter"}, provide={"collection": collection})
>>> index.create_index()  # returns immediately if the index already exists
>>> index.get_df("organized")
0
>>> index.get_df("organiz")
3048
# Next, a Searcher to query the index
>>> searcher = Searcher.create("BM25", {"hits": 3}, provide={"index": index})
>>> searcher.query("organized")
OrderedDict([('FBIS4-2046', 4.867800235748291),
             ('FBIS3-2553', 4.822000026702881),
             ('FBIS3-23578', 4.754199981689453)])
```

Finally, we can emulate the `RankTask.search()` method we called earlier:
```python
>>> results = {}
>>> for qid, topic in benchmark.topics['title'].items():
        results[qid] = searcher.query(topic)
```
To get metrics, we could then pass `results` to `capreolus.evaluator.eval_runs()`:
```eval_rst
.. autoapifunction:: capreolus.evaluator.eval_runs
```


## Creating New Modules

Capreolus modules implement the Capreolus module API plus an API specific to the module type.
The module API consists of four attributes:
- `module_type`: a string indicating the module's type, like "index" or "benchmark"
- `module_name`: a string indicating the module's name, like "anserini" or "robust04.yang19"
- `config_spec`: a list of `ConfigOption` objects, for example, `ConfigOption("stemmer", default_value="none", description="stemmer to use")`
- `dependencies` a list of `Dependency` objects; for example, `Dependency(key="collection", module="collection", name="robust04")`

When the module is created, any dependencies that are not explicitly passed with `provide={key: object}` are automatically created. The module's config options in `config_spec` and those of its dependencies are exposed as Capreolus configuration options.


### Task API

The `Task` module API specifies two additional class attributes: `commands` and `default_command`. These specify the functions that should serve as the Task's entrypoints and the default entrypoint, respectively.

Let's create a new task that mirrors the graph we constructed manually, except with two separate `Searcher` objects. We'll save the results from both searchers and measure their effectiveness on the validation queries to decide which searcher to report test set results on.

```python
from capreolus import evaluator, Dependency, ConfigOption
from capreolus.searcher import Searcher
from capreolus.task import Task
from capreolus.utils.loginit import get_logger

logger = get_logger(__name__)  # pylint: disable=invalid-name


@Task.register
class TutorialTask(Task):
    module_name = "tutorial"
    config_spec = [ConfigOption("optimize", "map", "metric to maximize on the validation set")]
    dependencies = [
        Dependency(
            key="benchmark", module="benchmark", name="robust04.yang19", provide_this=True, provide_children=["collection"]
        ),
        Dependency(key="searcher1", module="searcher", name="BM25RM3"),
        Dependency(key="searcher2", module="searcher", name="SDM"),
    ]

    commands = ["run"] + Task.help_commands
    default_command = "run"

    def run(self):
        output_dir = self.get_results_path()

        # read the title queries from the chosen benchmark's topic file
        results1 = self.searcher1.query_from_file(self.benchmark.topic_file, output_dir / "searcher1")
        results2 = self.searcher2.query_from_file(self.benchmark.topic_file, output_dir / "searcher2")
        searcher_results = [results1, results2]

        # using the benchmark's folds, which each contain train/validation/test queries,
        # choose the best run in `output_dir` for the fold based on the validation queries
        # and return metrics calculated on the test queries
        best_results = evaluator.search_best_run(
            searcher_results, self.benchmark, primary_metric=self.config["optimize"], metrics=evaluator.DEFAULT_METRICS
        )

        for fold, path in best_results["path"].items():
            shortpath = "..." + path[:-20]
            logger.info("fold=%s best run: %s", fold, shortpath)

        logger.info("cross-validated results when optimizing for '%s':", self.config["optimize"])
        for metric, score in sorted(best_results["score"].items()):
            logger.info("%15s: %0.4f", metric, score)

        return best_results

```
    