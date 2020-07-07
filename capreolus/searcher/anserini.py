import os
import json
import math
import subprocess
from collections import defaultdict

from tqdm import tqdm
import numpy as np

from capreolus import ConfigOption, Dependency, constants
from capreolus.utils.common import Anserini
from capreolus.utils.loginit import get_logger
from capreolus.utils.trec import load_trec_topics

from . import Searcher

logger = get_logger(__name__)  # pylint: disable=invalid-name
MAX_THREADS = constants["MAX_THREADS"]


def list2str(l, delimiter="-"):
    return delimiter.join(str(x) for x in l)


class AnseriniSearcherMixIn:
    """ MixIn for searchers that use Anserini's SearchCollection script """

    def _anserini_query_from_file(self, topicsfn, anserini_param_str, output_base_path, topicfield, rerank=False, run_fn=""):
        if not os.path.exists(topicsfn):
            raise IOError(f"could not find topics file: {topicsfn}")

        # for covid:
        field2querytype = {"query": "title", "question": "description", "narrative": "narrative"}
        for k, v in field2querytype.items():
            topicfield = topicfield.replace(k, v)

        donefn = os.path.join(output_base_path, "done")
        if os.path.exists(donefn):
            logger.debug(f"skipping Anserini SearchCollection call because path already exists: {donefn}")
            return

        # create index if it does not exist. the call returns immediately if the index does exist.
        self.index.create_index()

        os.makedirs(output_base_path, exist_ok=True)
        output_path = os.path.join(output_base_path, "searcher")

        # add stemmer and stop options to match underlying index
        indexopts = "-stemmer "
        indexopts += "none" if self.index.config["stemmer"] is None else self.index.config["stemmer"]
        if self.index.config["indexstops"]:
            indexopts += " -keepstopwords"


        index_path = self.index.get_index_path()
        anserini_fat_jar = Anserini.get_fat_jar()
        cmd = (
            f"java -classpath {anserini_fat_jar} "
            f"-Xms512M -Xmx31G -Dapp.name=SearchCollection io.anserini.search.SearchCollection "
            f"-topicreader Trec -index {index_path} {indexopts} -topics {topicsfn} -output {output_path} "
            f"-topicfield {topicfield} -inmem -threads {MAX_THREADS} {anserini_param_str}"
        )

        if rerank:
            anserini_fat_jar = "/home/xinyu1zhang/mpi-spring/anserini/target/anserini-0.9.1-SNAPSHOT-fatjar.jar"
            cmd = f"java -classpath {anserini_fat_jar} " \
                  f"-Xms512M -Xmx31G -Dapp.name=SimpleSearch io.anserini.search.SimpleSearcher " \
                  f"-topicreader Trec -index {index_path} -topics {topicsfn} -output {output_path} -rerank -runfile {run_fn} " \
                  f"-topicfield {topicfield}  -inmem -threads {MAX_THREADS} {anserini_param_str}"
            print("reranking: ", cmd)
        logger.info("Anserini writing runs to %s", output_path)
        logger.debug(cmd)

        app = subprocess.Popen(cmd.split(), stdout=subprocess.PIPE, universal_newlines=True)

        # Anserini output is verbose, so ignore DEBUG log lines and send other output through our logger
        for line in app.stdout:
            Anserini.filter_and_log_anserini_output(line, logger)

        app.wait()
        if app.returncode != 0:
            raise RuntimeError("command failed")

        with open(donefn, "wt") as donef:
            print("done", file=donef)


class PostprocessMixin:
    def _keep_topn(self, runs, topn):
        queries = sorted(list(runs.keys()), key=lambda k: int(k))
        for q in queries:
            docs = runs[q]
            if len(docs) <= topn:
                continue
            docs = sorted(docs.items(), key=lambda kv: kv[1], reverse=True)[:topn]
            runs[q] = {k: v for k, v in docs}
        return runs

    def filter(self, run_dir, docs_to_remove=None, docs_to_keep=None, topn=None):
        if (not docs_to_keep) and (not docs_to_remove):
            raise

        for fn in os.listdir(run_dir):
            if fn == "done":
                continue

            run_fn = os.path.join(run_dir, fn)
            self._filter(run_fn, docs_to_remove, docs_to_keep, topn)
        return run_dir

    def _filter(self, runfile, docs_to_remove, docs_to_keep, topn):
        runs = Searcher.load_trec_run(runfile)

        # filtering
        if docs_to_remove:  # prioritize docs_to_remove
            if isinstance(docs_to_remove, list):
                docs_to_remove = {q: docs_to_remove for q in runs}
            runs = {q: {d: v for d, v in docs.items() if d not in docs_to_remove.get(q, [])} for q, docs in runs.items()}
        elif docs_to_keep:
            if isinstance(docs_to_keep, list):
                docs_to_keep = {q: docs_to_keep for q in runs}
            runs = {q: {d: v for d, v in docs.items() if d in docs_to_keep[q]} for q, docs in runs.items()}

        if topn:
            runs = self._keep_topn(runs, topn)
        Searcher.write_trec_run(runs, runfile)  # overwrite runfile

    def dedup(self, run_dir, topn=None):
        for fn in os.listdir(run_dir):
            if fn == "done":
                continue
            run_fn = os.path.join(run_dir, fn)
            self._dedup(run_fn, topn)
        return run_dir

    def _dedup(self, runfile, topn):
        runs = Searcher.load_trec_run(runfile)
        new_runs = {q: {} for q in runs}

        # use the sum of each passage score as the document score, no sorting is done here
        for q, psg in runs.items():
            for pid, score in psg.items():
                docid = pid.split(".")[0]
                new_runs[q][docid] = max(new_runs[q].get(docid, -math.inf), score)
        runs = new_runs

        if topn:
            runs = self._keep_topn(runs, topn)
        Searcher.write_trec_run(runs, runfile)


@Searcher.register
class BM25(Searcher, AnseriniSearcherMixIn):
    """ Anserini BM25. This searcher's parameters can also be specified as lists indicating parameters to grid search (e.g., ``"0.4,0.6,0.8"`` or ``"0.4..1,0.2"``). """

    module_name = "BM25"

    dependencies = [Dependency(key="index", module="index", name="anserini")]
    config_spec = [
        ConfigOption("k1", 0.9, "controls term saturation", value_type="floatlist"),
        ConfigOption("b", 0.4, "controls document length normalization", value_type="floatlist"),
        ConfigOption("hits", 1000, "number of results to return"),
        ConfigOption("fields", "title"),
    ]

    def _query_from_file(self, topicsfn, output_path, config):
        """
        Runs BM25 search. Takes a query from the topic files, and fires it against the index
        Args:
            topicsfn: Path to a topics file
            output_path: Path where the results of the search (i.e the run file) should be stored

        Returns: Path to the run file where the results of the search are stored

        """
        bstr, k1str = list2str(config["b"], delimiter=" "), list2str(config["k1"], delimiter=" ")
        hits = config["hits"]
        anserini_param_str = f"-bm25 -bm25.b {bstr} -bm25.k1 {k1str} -hits {hits}"
        self._anserini_query_from_file(topicsfn, anserini_param_str, output_path, config["fields"])

        return output_path


@Searcher.register
class BM25Grid(Searcher, AnseriniSearcherMixIn):
    """ Deprecated. BM25 with a grid search for k1 and b. Search is from 0.1 to bmax/k1max in 0.1 increments """

    module_name = "BM25Grid"
    dependencies = [Dependency(key="index", module="index", name="anserini")]
    config_spec = [
        ConfigOption("k1max", 1.0, "maximum k1 value to include in grid search (starting at 0.1)"),
        ConfigOption("bmax", 1.0, "maximum b value to include in grid search (starting at 0.1)"),
        ConfigOption("hits", 1000, "number of results to return"),
        ConfigOption("fields", "title"),
    ]

    def _query_from_file(self, topicsfn, output_path, config):
        bs = np.around(np.arange(0.1, config["bmax"] + 0.1, 0.1), 1)
        k1s = np.around(np.arange(0.1, config["k1max"] + 0.1, 0.1), 1)
        bstr = " ".join(str(x) for x in bs)
        k1str = " ".join(str(x) for x in k1s)
        hits = config["hits"]
        anserini_param_str = f"-bm25 -bm25.b {bstr} -bm25.k1 {k1str} -hits {hits}"

        self._anserini_query_from_file(topicsfn, anserini_param_str, output_path, config["fields"])

        return output_path


@Searcher.register
class BM25RM3(Searcher, AnseriniSearcherMixIn):
    """ Anserini BM25 with RM3 expansion. This searcher's parameters can also be specified as lists indicating parameters to grid search (e.g., ``"0.4,0.6,0.8"`` or ``"0.4..1,0.2"``). """

    module_name = "BM25RM3"
    dependencies = [Dependency(key="index", module="index", name="anserini")]
    config_spec = [
        ConfigOption("k1", [0.65, 0.70, 0.75], "controls term saturation", value_type="floatlist"),
        ConfigOption("b", [0.60, 0.7], "controls document length normalization", value_type="floatlist"),
        ConfigOption("fbTerms", [65, 70, 95, 100], "number of generated terms from feedback", value_type="intlist"),
        ConfigOption("fbDocs", [5, 10, 15], "number of documents used for feedback", value_type="intlist"),
        ConfigOption("originalQueryWeight", [0.5], "the weight of unexpended query", value_type="floatlist"),
        ConfigOption("hits", 1000, "number of results to return"),
        ConfigOption("fields", "title"),
    ]

    def query_from_file(self, topicsfn, output_path, rerank=False, run_fn=""):
        return self._query_from_file(topicsfn, output_path, self.config, rerank=rerank, run_fn=run_fn)

    def _query_from_file(self, topicsfn, output_path, config, rerank=False, run_fn=""):
        hits = str(config["hits"])

        anserini_param_str = (
            "-rm3 "
            + " ".join(f"-rm3.{k} {list2str(config[k], ' ')}" for k in ["fbTerms", "fbDocs", "originalQueryWeight"])
            + " -bm25 "
            + " ".join(f"-bm25.{k} {list2str(config[k], ' ')}" for k in ["k1", "b"])
            + f" -hits {hits}"
        )
        self._anserini_query_from_file(topicsfn, anserini_param_str, output_path, config["fields"], rerank=rerank, run_fn=run_fn)

        return output_path


@Searcher.register
class BM25Reranker(Searcher):
    module_name = "BM25_reranker"
    dependencies = [
        Dependency(key="index", module="index", name="anserini_tf"),
        # "searcher": Dependency(module="searcher", name="csn_distractors"),
    ]
    config_spec = [
        ConfigOption("k1", [0.65, 0.70, 0.75], "controls term saturation", value_type="floatlist"),
        ConfigOption("b", [0.60, 0.7], "controls document length normalization", value_type="floatlist"),
        ConfigOption("hits", 1000, "number of results to return"),
        ConfigOption("fields", "title"),
    ]

    def __calc_bm25(self, query, docid):
        doclen = self.index.get_doclen(docid)
        k1_b = [(float(k1), float(b)) for k1 in self.config["k1"] for b in self.config["b"]]

        tfs = {term: self.index.get_tf(term, docid) for term in query}
        idfs = {term: self.index.get_idf(term) for term in query}
        bm25_per_qterm = {f"k1={k1},b={b}": (docid, sum(
            [idfs[term] * tfs[term] / (tfs[term] + k1 * (1 - b + b * doclen / self.avg_doc_len)) for term in query]))
            for k1, b in k1_b}
        return bm25_per_qterm

    def calc_bm25(self, query, docids):
        kb_docid_scores = [self.__calc_bm25(query, docid) for docid in docids]  # {runname: {docid: score}}
        kbs = kb_docid_scores[0].keys()
        bm25s = {kb: {r_idx_score[kb][0]: r_idx_score[kb][1] for r_idx_score in kb_docid_scores} for kb in kbs}

        for kb in bm25s:
            if self.config["hits"] >= len(bm25s[kb]):
                continue
            sorted_bm25 = sorted(bm25s[kb].items(), key=lambda k_v: k_v[1], reverse=True)
            bm25s[kb] = {docid: score for docid, score in sorted_bm25[:self.config["hits"]]}

        return bm25s

    def query_from_file(self, topicsfn, output_path, runs=None):
        """ only perform bm25 on the docs in runs """
        donefn = os.path.join(output_path, "done")
        if os.path.exists(donefn):
            logger.debug(f"done file for {self.module_name} already exists, skip search")
            return output_path

        self.index.open()

        # prepare topic
        cache_fn = self.get_cache_path()
        topic_cache_path = cache_fn / "topic.analyze.json"
        docnos = self.index.collection.get_docnos()
        self.avg_doc_len = self.index.get_avglen()

        cache_fn.mkdir(exist_ok=True, parents=True)
        output_path.mkdir(exist_ok=True, parents=True)

        if os.path.exists(topic_cache_path):
            topics = json.load(open(topic_cache_path))
            logger.info(f"loading analyzed topic from cache {topic_cache_path}")
        else:
            topics = load_trec_topics(topicsfn)["title"]
            topics = {qid: self.index.analyze_sent(q) for qid, q in tqdm(topics.items(), desc="Transforming query")}
            json.dump(topics, open(topic_cache_path, "w"))
            logger.info(f"storing analyzed topic from cache {topic_cache_path}")

        if isinstance(runs, dict):  # filter undesired query if runs are given
            topics = [(qid, query) for qid, query in topics.items() if qid in runs]

        mode = "w"
        for qid, query in tqdm(topics, desc=f"Calculating bm25"):
            docids = runs[qid] if runs else docnos
            if not docids:
                continue

            runname_bm25 = self.calc_bm25(query, docids)
            for runname, bm25 in runname_bm25.items():
                self.write_trec_run({qid: bm25}, os.path.join(output_path, f"searcher_{runname}"), mode)
            mode = "a"

        with open(donefn, "wt") as donef:
            print("done", file=donef)

        return output_path


@Searcher.register
class BM25PostProcess(BM25, PostprocessMixin):
    module_name = "BM25Postprocess"

    config_spec = [
        ConfigOption("k1", 0.9, "controls term saturation", value_type="floatlist"),
        ConfigOption("b", 0.4, "controls document length normalization", value_type="floatlist"),
        ConfigOption("hits", 1000, "number of results to return"),
        ConfigOption("topn", 1000),
        ConfigOption("fields", "title"),
        ConfigOption("dedep", False),
    ]

    def query_from_file(self, topicsfn, output_path, docs_to_remove=None):
        output_path = super().query_from_file(topicsfn, output_path)  # will call _query_from_file() from BM25

        if docs_to_remove:
            output_path = self.filter(output_path, docs_to_remove=docs_to_remove, topn=self.config["topn"])
        if self.config["dedup"]:
            output_path = self.dedup(output_path, topn=self.config["topn"])

        return output_path


@Searcher.register
class StaticBM25RM3Rob04Yang19(Searcher):
    """ Tuned BM25+RM3 run used by Yang et al. in [1]. This should be used only with a benchmark using the same folds and queries.

        [1] Wei Yang, Kuang Lu, Peilin Yang, and Jimmy Lin. Critically Examining the "Neural Hype": Weak Baselines and  the Additivity of Effectiveness Gains from Neural Ranking Models. SIGIR 2019.
    """

    module_name = "bm25staticrob04yang19"

    def _query_from_file(self, topicsfn, output_path, config):
        import shutil

        outfn = os.path.join(output_path, "static.run")
        if not os.path.exists(outfn):
            os.makedirs(output_path, exist_ok=True)
            shutil.copy2(constants["PACKAGE_PATH"] / "data" / "rob04_yang19_rm3.run", outfn)

        return output_path

    def query(self, *args, **kwargs):
        raise NotImplementedError("this searcher uses a static run file, so it cannot handle new queries")


@Searcher.register
class BM25PRF(Searcher, AnseriniSearcherMixIn):
    """ Anserini BM25 PRF. This searcher's parameters can also be specified as lists indicating parameters to grid search (e.g., ``"0.4,0.6,0.8"`` or ``"0.4..1,0.2"``). """

    module_name = "BM25PRF"

    dependencies = [Dependency(key="index", module="index", name="anserini")]
    config_spec = [
        ConfigOption("k1", [0.65, 0.70, 0.75], "controls term saturation", value_type="floatlist"),
        ConfigOption("b", [0.60, 0.7], "controls document length normalization", value_type="floatlist"),
        ConfigOption("fbTerms", [65, 70, 95, 100], "number of generated terms from feedback", value_type="intlist"),
        ConfigOption("fbDocs", [5, 10, 15], "number of documents used for feedback", value_type="intlist"),
        ConfigOption("newTermWeight", [0.2, 0.25], value_type="floatlist"),
        ConfigOption("hits", 1000, "number of results to return"),
        ConfigOption("fields", "title"),
    ]

    def _query_from_file(self, topicsfn, output_path, config):
        hits = str(config["hits"])

        anserini_param_str = (
            "-bm25prf "
            + " ".join(f"-bm25prf.{k} {list2str(config[k], ' ')}" for k in ["fbTerms", "fbDocs", "newTermWeight", "k1", "b"])
            + " -bm25 "
            + " ".join(f"-bm25.{k} {list2str(config[k], ' ')}" for k in ["k1", "b"])
            + f" -hits {hits}"
        )
        self._anserini_query_from_file(topicsfn, anserini_param_str, output_path, config["fields"])

        return output_path


@Searcher.register
class AxiomaticSemanticMatching(Searcher, AnseriniSearcherMixIn):
    """ Anserini BM25 with Axiomatic query expansion. This searcher's parameters can also be specified as lists indicating parameters to grid search (e.g., ``"0.4,0.6,0.8"`` or ``"0.4..1,0.2"``). """

    module_name = "axiomatic"
    dependencies = [Dependency(key="index", module="index", name="anserini")]
    config_spec = [
        ConfigOption("k1", 0.9, "controls term saturation", value_type="floatlist"),
        ConfigOption("b", 0.4, "controls document length normalization", value_type="floatlist"),
        ConfigOption("r", 20, value_type="intlist"),
        ConfigOption("n", 30, value_type="intlist"),
        ConfigOption("beta", 0.4, value_type="floatlist"),
        ConfigOption("top", 20, value_type="intlist"),
        ConfigOption("hits", 1000, "number of results to return"),
        ConfigOption("fields", "title"),
    ]

    def _query_from_file(self, topicsfn, output_path, config):
        hits = str(config["hits"])

        anserini_param_str = "-axiom -axiom.deterministic -axiom.r {0} -axiom.n {1} -axiom.beta {2} -axiom.top {3}".format(
            *[list2str(config[k], " ") for k in ["r", "n", "beta", "top"]]
        )
        anserini_param_str += " -bm25 -bm25.k1 {0} -bm25.b {1} ".format(*[list2str(config[k], " ") for k in ["k1", "b"]])
        anserini_param_str += f" -hits {hits}"
        self._anserini_query_from_file(topicsfn, anserini_param_str, output_path, config["fields"])

        return output_path


@Searcher.register
class DirichletQL(Searcher, AnseriniSearcherMixIn):
    """ Anserini QL with Dirichlet smoothing. This searcher's parameters can also be specified as lists indicating parameters to grid search (e.g., ``"0.4,0.6,0.8"`` or ``"0.4..1,0.2"``). """

    module_name = "DirichletQL"
    dependencies = [Dependency(key="index", module="index", name="anserini")]

    config_spec = [
        ConfigOption("mu", 1000, "smoothing parameter", value_type="intlist"),
        ConfigOption("hits", 1000, "number of results to return"),
        ConfigOption("fields", "title"),
    ]

    def _query_from_file(self, topicsfn, output_path, config):
        """
        Runs Dirichlet QL search. Takes a query from the topic files, and fires it against the index
        Args:
            topicsfn: Path to a topics file
            output_path: Path where the results of the search (i.e the run file) should be stored

        Returns: Path to the run file where the results of the search are stored

        """
        mustr = list2str(config["mu"], delimiter=" ")
        hits = config["hits"]
        anserini_param_str = f"-qld -qld.mu {mustr} -hits {hits}"
        self._anserini_query_from_file(topicsfn, anserini_param_str, output_path, config["fields"])

        return output_path


@Searcher.register
class QLJM(Searcher, AnseriniSearcherMixIn):
    """ Anserini QL with Jelinek-Mercer smoothing. This searcher's parameters can also be specified as lists indicating parameters to grid search (e.g., ``"0.4,0.6,0.8"`` or ``"0.4..1,0.2"``). """

    module_name = "QLJM"
    dependencies = [Dependency(key="index", module="index", name="anserini")]
    config_spec = [
        ConfigOption("lam", 0.1, value_type="floatlist"),
        ConfigOption("hits", 1000, "number of results to return"),
        ConfigOption("fields", "title"),
    ]

    def _query_from_file(self, topicsfn, output_path, config):
        anserini_param_str = "-qljm -qljm.lambda {0} -hits {1}".format(list2str(config["lam"], delimiter=" "), config["hits"])

        self._anserini_query_from_file(topicsfn, anserini_param_str, output_path, config["fields"])

        return output_path


@Searcher.register
class INL2(Searcher, AnseriniSearcherMixIn):
    """ Anserini I(n)L2 scoring model. This searcher does not support list parameters. """

    module_name = "INL2"
    dependencies = [Dependency(key="index", module="index", name="anserini")]
    config_spec = [
        ConfigOption("c", 0.1),  # array input of this parameter is not support by anserini.SearchCollection
        ConfigOption("hits", 1000, "number of results to return"),
        ConfigOption("fields", "title"),
    ]

    def _query_from_file(self, topicsfn, output_path, config):
        anserini_param_str = "-inl2 -inl2.c {0} -hits {1}".format(config["c"], config["hits"])
        self._anserini_query_from_file(topicsfn, anserini_param_str, output_path, config["fields"])
        return output_path


@Searcher.register
class SPL(Searcher, AnseriniSearcherMixIn):
    """
    Anserini SPL scoring model. This searcher does not support list parameters.
    """

    module_name = "SPL"
    dependencies = [Dependency(key="index", module="index", name="anserini")]

    config_spec = [
        ConfigOption("c", 0.1),  # array input of this parameter is not support by anserini.SearchCollection
        ConfigOption("hits", 1000, "number of results to return"),
        ConfigOption("fields", "title"),
    ]

    def _query_from_file(self, topicsfn, output_path, config):
        anserini_param_str = "-spl -spl.c {0} -hits {1}".format(config["c"], config["hits"])

        self._anserini_query_from_file(topicsfn, anserini_param_str, output_path, config["fields"])

        return output_path


@Searcher.register
class F2Exp(Searcher, AnseriniSearcherMixIn):
    """
    F2Exp scoring model. This searcher does not support list parameters.
    """

    module_name = "F2Exp"
    dependencies = [Dependency(key="index", module="index", name="anserini")]

    config_spec = [
        ConfigOption("s", 0.5),  # array input of this parameter is not support by anserini.SearchCollection
        ConfigOption("hits", 1000, "number of results to return"),
        ConfigOption("fields", "title"),
    ]

    def _query_from_file(self, topicsfn, output_path, config):
        anserini_param_str = "-f2exp -f2exp.s {0} -hits {1}".format(config["s"], config["hits"])

        self._anserini_query_from_file(topicsfn, anserini_param_str, output_path, config["fields"])

        return output_path


@Searcher.register
class F2Log(Searcher, AnseriniSearcherMixIn):
    """
    F2Log scoring model. This searcher does not support list parameters.
    """

    module_name = "F2Log"
    dependencies = [Dependency(key="index", module="index", name="anserini")]

    config_spec = [
        ConfigOption("s", 0.5),  # array input of this parameter is not support by anserini.SearchCollection
        ConfigOption("hits", 1000, "number of results to return"),
        ConfigOption("fields", "title"),
    ]

    def _query_from_file(self, topicsfn, output_path, config):
        anserini_param_str = "-f2log -f2log.s {0} -hits {1}".format(config["s"], config["hits"])

        self._anserini_query_from_file(topicsfn, anserini_param_str, output_path, config["fields"])

        return output_path


@Searcher.register
class SDM(Searcher, AnseriniSearcherMixIn):
    """
    Anserini BM25 with the Sequential Dependency Model. This searcher supports list parameters for only k1 and b.
    """

    module_name = "SDM"
    dependencies = [Dependency(key="index", module="index", name="anserini")]

    # array input of (tw, ow, uw) is not support by anserini.SearchCollection
    config_spec = [
        ConfigOption("k1", 0.9, "controls term saturation", value_type="floatlist"),
        ConfigOption("b", 0.4, "controls document length normalization", value_type="floatlist"),
        ConfigOption("tw", 0.85, "term weight"),
        ConfigOption("ow", 0.15, "ordered window weight"),
        ConfigOption("uw", 0.05, "unordered window weight"),
        ConfigOption("hits", 1000, "number of results to return"),
        ConfigOption("fields", "title"),
    ]

    def _query_from_file(self, topicsfn, output_path, config):
        hits = config["hits"]
        anserini_param_str = "-sdm -sdm.tw {0} -sdm.ow {1} -sdm.uw {2}".format(*[config[k] for k in ["tw", "ow", "uw"]])
        anserini_param_str += " -bm25 -bm25.k1 {0} -bm25.b {1}".format(*[list2str(config[k], " ") for k in ["k1", "b"]])
        anserini_param_str += f" -hits {hits}"
        self._anserini_query_from_file(topicsfn, anserini_param_str, output_path, config["fields"])

        return output_path


@Searcher.register
class CodeSearchDistractor(Searcher):
    """ Providing the 999 distractor documents """
    module_name = "csn_distractors"
    dependencies = [
        Dependency(key="benchmark", module="benchmark", name="codesearchnet_corpus")
    ]
    config_spec = [
        ConfigOption("includetrain", False, "Whether to prepare training data")
    ]

    def query_from_file(self, topicsfn, output_path):
        donefn = os.path.join(output_path, "done")
        if os.path.exists(donefn):
            logger.debug(f"done file for {self.module_name} already exists, skip search")
            return str(output_path)

        runs = defaultdict(dict)
        raw_dir = self.benchmark.collection.download_raw()
        sets = ["train", "valid", "test"] if self.config["includetrain"] else ["valid", "test"]
        for set_name in sets:
            objs = []
            set_path = raw_dir / self.benchmark.collection.config["lang"] / "final" / "jsonl" / set_name
            for data in self.benchmark.generate_parsed_doc_from_gz(set_path):
                objs.append(data)

                if len(objs) == 1000:  # 1 ground truth and 999 distractor docs
                    for obj1 in objs:
                        qid = self.benchmark.get_qid(obj1["docstring_raw"])
                        gt_docid = self.benchmark.get_docid(obj1["url"], obj1["code_raw"])
                        assert qid != -1 and gt_docid != -1
                        all_docs = []

                        for rank, obj2 in enumerate(objs):
                            docid = self.benchmark.get_docid(obj2["url"], obj2["code_raw"])
                            assert docid != 1

                            all_docs.append(docid)
                            runs[qid][docid] = 1.0 / (rank + 1)
                        assert gt_docid in all_docs
                    objs = []  # reset

        os.makedirs(output_path, exist_ok=True)
        self.write_trec_run(runs, os.path.join(output_path, "searcher"))

        with open(donefn, "wt") as donef:
            print("done", file=donef)
        return str(output_path)

    def query(self, *args, **kwargs):
        raise NotImplementedError("this searcher uses a static run file, so it cannot handle new queries")
