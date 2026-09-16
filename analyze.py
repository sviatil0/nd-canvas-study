"""Cross-reference exam/practice problems vs homework/in-class material.

Identifies topics that appear frequently in exams + practice but rarely in
homeworks + in-class notes. These are likely under-prepared areas.

Usage:
    python analyze.py --course-dir downloads/128781_statistics
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

# Per-course topic keyword dictionaries. Each topic has a list of regex aliases.

_TOPICS_STATS = {
    "descriptive_statistics":   [r"mean", r"median", r"mode", r"variance", r"standard deviation", r"quartile", r"percentile", r"boxplot", r"histogram", r"stem.{0,3}leaf"],
    "probability_basics":       [r"sample space", r"\bevent\b", r"complement", r"\bunion\b", r"intersection", r"mutually exclusive"],
    "conditional_probability":  [r"conditional probab", r"P\(.*\|.*\)", r"bayes"],
    "independence":             [r"\bindependent\b", r"independence"],
    "discrete_distributions":   [r"binomial", r"poisson", r"geometric", r"hypergeometric", r"bernoulli", r"negative binomial"],
    "continuous_distributions": [r"\bnormal\b", r"gaussian", r"exponential distribut", r"\buniform\b", r"\bgamma\b", r"\bbeta\b distribut", r"weibull", r"lognormal"],
    "expected_value":           [r"expected value", r"E\(X\)", r"E\[X\]", r"variance of"],
    "joint_distributions":      [r"joint distribut", r"marginal distribut", r"covariance", r"correlation"],
    "sampling_distributions":   [r"sampling distribut", r"central limit", r"\bCLT\b", r"\bsampling\b"],
    "point_estimation":         [r"point estimat", r"\bMLE\b", r"maximum likelihood", r"unbiased", r"method of moments"],
    "confidence_intervals":     [r"confidence interval", r"\bCI\b", r"margin of error"],
    "hypothesis_testing":       [r"hypothesis test", r"null hypothesis", r"alternative hypothesis", r"p-?value", r"reject", r"type I error", r"type II error", r"power of"],
    "t_test":                   [r"\bt-?test\b", r"t-?statistic", r"student.{0,3}t"],
    "z_test":                   [r"\bz-?test\b", r"z-?statistic", r"z-?score"],
    "chi_squared":              [r"chi.{0,3}squar", r"\bchi-?sq\b", r"χ.?2", r"goodness of fit"],
    "anova":                    [r"\bANOVA\b", r"analysis of variance", r"F-?test", r"F-?statistic", r"one-?way", r"two-?way"],
    "two_sample":               [r"two[- ]sample", r"paired", r"pooled", r"difference of means", r"difference of proportions"],
    "regression_simple":        [r"simple.{0,4}regression", r"linear regression", r"least squares", r"slope", r"intercept", r"residual"],
    "regression_multiple":      [r"multiple regression", r"multicollinearity", r"adjusted R", r"R-?squared"],
    "regression_assumptions":   [r"homoscedastic", r"heteroscedastic", r"normality of residual", r"linearity assump", r"\bQ-?Q plot\b"],
    "correlation":              [r"\bcorrelation\b", r"pearson", r"spearman", r"\br\^?2\b"],
    "nonparametric":            [r"nonparametric", r"rank.{0,4}sum", r"wilcoxon", r"sign test", r"mann.{0,3}whitney"],
    "categorical_data":         [r"contingency table", r"categorical", r"proportion test"],
}

_TOPICS_COMP_ARCH = {
    "isa_basics":               [r"\bISA\b", r"instruction set", r"opcode", r"register file", r"datapath", r"control unit", r"\bALU\b"],
    "performance_metrics":      [r"\bCPI\b", r"\bMIPS\b", r"clock cycle", r"clock rate", r"amdahl", r"speedup", r"execution time", r"\bIPC\b"],
    "riscv_instructions":       [r"RISC.?V", r"\baddi?\b", r"\blw\b", r"\bsw\b", r"\bbeq\b", r"\bbne\b", r"\bjal\b", r"\bjalr\b", r"r-?type", r"i-?type", r"s-?type"],
    "riscv_encoding":           [r"opcode field", r"funct3", r"funct7", r"immediate field", r"sign.?extend", r"instruction format", r"u-?type", r"b-?type", r"j-?type"],
    "riscv_control_procedures": [r"\bra\b", r"\bsp\b", r"stack frame", r"calling convention", r"caller.?save", r"callee.?save", r"return address", r"prologue", r"epilogue", r"function call"],
    "pipelining_intro":         [r"pipeline", r"\bIF\b", r"\bID\b", r"\bEX\b", r"\bMEM\b", r"\bWB\b", r"pipeline register", r"throughput"],
    "pipeline_data_hazards":    [r"data hazard", r"\bRAW\b", r"\bWAR\b", r"\bWAW\b", r"forwarding", r"bypassing", r"load.?use hazard", r"\bstall\b"],
    "pipeline_control_hazards": [r"control hazard", r"branch hazard", r"branch penalty", r"\bflush\b", r"delayed branch", r"branch resolution"],
    "memory_hierarchy_intro":   [r"memory hierarchy", r"\bSRAM\b", r"\bDRAM\b", r"locality", r"temporal locality", r"spatial locality", r"working set"],
    "direct_mapped_cache":      [r"direct.?mapped", r"cache line", r"cache block", r"\btag\b", r"\bindex\b", r"\boffset\b", r"valid bit"],
    "associative_cache":        [r"set.?associative", r"fully.?associative", r"\bway\b", r"replacement policy", r"\bLRU\b", r"\bFIFO\b", r"random replacement"],
    "cache_performance":        [r"\bAMAT\b", r"average memory access", r"hit rate", r"miss rate", r"hit time", r"miss penalty", r"compulsory miss", r"capacity miss", r"conflict miss"],
    "cache_aware_programming":  [r"loop tiling", r"loop blocking", r"cache.?aware", r"cache.?friendly", r"row.?major", r"column.?major", r"prefetch"],
    "virtual_memory_intro":     [r"virtual memory", r"physical address", r"virtual address", r"address translation", r"\bMMU\b"],
    "vm_paging":                [r"page table", r"page size", r"\bPTE\b", r"page fault", r"page frame", r"\bVPN\b", r"\bPPN\b", r"multi.?level page"],
    "vm_tlb_performance":       [r"\bTLB\b", r"translation lookaside buffer", r"TLB hit", r"TLB miss", r"TLB reach"],
    "branch_prediction":        [r"branch predict", r"\bBHT\b", r"branch history", r"2.?bit predictor", r"correlating predictor", r"tournament predictor", r"\bBTB\b"],
    "scheduling_intro":         [r"instruction.?level parallelism", r"\bILP\b", r"static scheduling", r"in.?order", r"out.?of.?order", r"loop unroll"],
    "dynamic_scheduling":       [r"tomasulo", r"reservation station", r"reorder buffer", r"\bROB\b", r"register renaming", r"common data bus", r"\bCDB\b", r"speculative execution"],
    "parallelism_intro":        [r"\bSMP\b", r"multiprocessor", r"shared memory", r"thread.?level parallelism", r"\bTLP\b", r"multicore", r"\bSIMD\b", r"\bMIMD\b"],
    "cache_coherence":          [r"cache coherence", r"\bMESI\b", r"\bMSI\b", r"snooping", r"snoopy", r"directory protocol", r"false sharing", r"write.?invalidate", r"write.?update", r"coherence protocol"],
}

_TOPICS_PARADIGMS = {
    "course_overview":          [r"\bparadigm\b", r"course\s+overview", r"programming.*paradigm"],
    "javascript_basics":        [r"\bvar\b", r"\blet\b", r"\bconst\b", r"\bhoisting\b", r"javascript.*type", r"\bNaN\b", r"undefined", r"strict\s+mode"],
    "javascript_closures":      [r"\bclosure\b", r"\blexical\s+scope", r"this\s+keyword", r"\bIIFE\b", r"deep\s+binding", r"shallow\s+binding"],
    "javascript_objects":       [r"\bprototype\b", r"prototypal", r"Object\.create", r"class\s+extends", r"new\s+target"],
    "javascript_async":         [r"\bpromise\b", r"\basync\b", r"\bawait\b", r"event\s+loop", r"setTimeout", r"\bcallback\b", r"microtask", r"macrotask"],
    "frontend_dom":             [r"\bDOM\b", r"document\.querySelector", r"addEventListener", r"event\s+listener", r"event.driven"],
    "frontend_mvc":             [r"\bMVC\b", r"model.view.controller", r"\bview\b\s+component", r"\bcontroller\b\s+component"],
    "exam1_review":             [r"exam\s*1\s*review", r"midterm\s*1.*review"],
    "python_basics":            [r"\bdef\s+\w+", r"\bself\b", r"python.*scope", r"GIL", r"duck\s+typing", r"strongly.?typed", r"dynamically.?typed"],
    "python_advanced":          [r"\bdecorator\b", r"@\w+", r"list\s+comprehension", r"\byield\b", r"generator", r"context\s+manager", r"with\s+open"],
    "django_intro":             [r"\bdjango\b", r"manage\.py", r"settings\.py", r"\bMVT\b", r"model.template.view"],
    "django_models":            [r"models\.py", r"models\.Model", r"makemigrations", r"\bORM\b", r"ForeignKey", r"queryset"],
    "django_views_templates":   [r"views\.py", r"render\(.*template", r"urls\.py", r"url\s+pattern", r"template\s+tag"],
    "django_forms_auth":        [r"forms\.py", r"login_required", r"@login_required", r"\bsessions\b", r"\bauth\b\s+(decorator|middleware)", r"CSRF"],
    "rest_apis":                [r"\bREST\b", r"\bRESTful\b", r"HTTP\s+(GET|POST|PUT|DELETE|PATCH)", r"stateless", r"\bidempotent\b", r"resource\s+representation"],
    "exam2_review":             [r"exam\s*2\s*review", r"midterm\s*2.*review"],
    "java_basics":              [r"\bJava\b", r"public\s+static\s+void\s+main", r"\bJVM\b", r"\.java\b", r"javac", r"static.?typed"],
    "java_oop":                 [r"\bextends\b", r"\binterface\b", r"\babstract\b", r"\bpolymorphism\b", r"late\s+binding", r"method\s+overriding", r"\binheritance\b"],
    "java_collections":         [r"java\.util\b", r"ArrayList", r"HashMap", r"\bgenerics?\b", r"<T>", r"equals\(.*Object", r"hashCode\(\)"],
    "java_concurrency":         [r"java\.util\.concurrent", r"\bThread\b", r"\bRunnable\b", r"\bsynchronized\b", r"ExecutorService", r"\bAtomicInteger\b"],
    "clojure_intro":            [r"\bClojure\b", r"\bdefn\b", r"\(def\s+", r"S-?expression", r"\bLisp\b"],
    "clojure_immutability":    [r"immutable", r"persistent\s+data", r"\brecur\b", r"lazy\s+seq", r"lazy\s+evaluation"],
    "functional_higher_order":  [r"\bmap\b\s*\(", r"\bfilter\b\s*\(", r"\breduce\b\s*\(", r"higher.?order", r"first.?class\s+function", r"pure\s+function"],
    "paradigms_comparison":     [r"imperative", r"declarative", r"object.?oriented", r"functional\s+programming", r"event.driven\s+programming"],
    "binding_typing":           [r"static\s+typing", r"dynamic\s+typing", r"strong.?typed", r"weak.?typed", r"deep\s+binding", r"shallow\s+binding", r"variable\s+hoisting"],
}

# CSE 40657 — Natural Language Processing (cid 145184), Bang Nguyen, fall 2026.
# Topic keys follow the instructor's 15-week schedule on bnguyen5.github.io.
_TOPICS_NLP = {
    "nlp_overview":             [r"\bNLP\b", r"natural language processing", r"language technolog", r"course overview", r"\bsyllabus\b"],
    "ml_foundations":           [r"supervised learning", r"train.{0,10}test split", r"\bfeatures?\b", r"logistic regression", r"naive bayes", r"gradient descent", r"loss function", r"cross.?entropy", r"overfit"],
    "tokenization":             [r"token(?:ize|ization|izer)", r"\bBPE\b", r"byte.?pair", r"wordpiece", r"sentencepiece", r"subword", r"\blemma", r"stemming", r"\bmorpholog", r"\bregex\b", r"edit distance", r"\btype[s]? .{0,10}token"],
    "ngram_language_models":    [r"\bn-?gram\b", r"bigram", r"trigram", r"unigram", r"markov assumption", r"\bsmoothing\b", r"laplace", r"add.?one", r"kneser.?ney", r"backoff", r"interpolat"],
    "lm_evaluation":            [r"\bperplexity\b", r"held.?out", r"\bBLEU\b", r"\bROUGE\b", r"\bF1\b", r"precision.{0,10}recall", r"intrinsic evaluation", r"extrinsic evaluation", r"\baccuracy\b"],
    "neural_networks":          [r"neural network", r"feed.?forward", r"\bMLP\b", r"backprop", r"activation function", r"\bReLU\b", r"\bsoftmax\b", r"\bPyTorch\b", r"\btensor\b", r"embedding layer"],
    "neural_language_models":   [r"neural language model", r"word2vec", r"\bGloVe\b", r"skip.?gram", r"\bCBOW\b", r"word embedding", r"distributional semantic", r"\bRNN\b", r"\bLSTM\b", r"\bGRU\b", r"vanishing gradient"],
    "pos_tagging_parsing":      [r"part.?of.?speech", r"\bPOS tag", r"\bHMM\b", r"hidden markov", r"viterbi", r"\bCRF\b", r"sequence label", r"\bBIO\b tag", r"named entity", r"\bNER\b", r"constituency", r"dependency pars", r"\bCKY\b", r"context.?free grammar", r"\bPCFG\b", r"treebank"],
    "encoder_decoder_attention":[r"encoder.?decoder", r"seq2seq", r"sequence.?to.?sequence", r"\battention\b", r"attention weight", r"\bcontext vector\b", r"beam search", r"teacher forcing"],
    "machine_translation":      [r"machine translation", r"\bMT\b", r"parallel corpus", r"alignment model", r"\bIBM model", r"\bBLEU\b", r"back.?translation", r"source.{0,10}target language"],
    "transformers":             [r"\btransformer\b", r"self.?attention", r"multi.?head", r"positional encoding", r"query.{0,5}key.{0,5}value", r"layer norm", r"residual connection", r"\bBERT\b", r"\bGPT\b", r"masked language model"],
    "llm_training":             [r"large language model", r"\bLLM\b", r"pre.?train", r"scaling law", r"\btokens? budget\b", r"data curation", r"distributed training", r"mixed precision", r"\bcheckpoint"],
    "llm_posttraining":         [r"post.?train", r"instruction tun", r"fine.?tun", r"\bRLHF\b", r"reward model", r"\bDPO\b", r"preference optimization", r"alignment", r"\bLoRA\b", r"parameter.?efficient", r"\bPEFT\b", r"chain.?of.?thought", r"in.?context learning", r"few.?shot", r"prompt"],
    "semantics_retrieval":      [r"semantic", r"word sense", r"\bWSD\b", r"semantic role", r"\bSRL\b", r"coreference", r"question answering", r"\bretrieval\b", r"\bRAG\b", r"dense retriev", r"\bBM25\b", r"vector (?:store|database|index)", r"\bknowledge base\b"],
    "evaluation_interpretability": [r"interpretab", r"explainab", r"probing", r"attention visuali", r"saliency", r"\bablation\b", r"benchmark", r"human evaluation", r"\bMMLU\b", r"\bGLUE\b", r"contamination"],
    "responsible_nlp":          [r"\bbias\b", r"fairness", r"\btoxicity\b", r"\bharm", r"hallucinat", r"privacy", r"responsible (?:AI|NLP)", r"ethic", r"\bmisinformation\b", r"data provenance"],
    "multilingual_lowresource": [r"multilingual", r"cross.?lingual", r"low.?resource", r"zero.?shot transfer", r"\btransfer learning\b", r"language famil", r"\bcode.?switch"],
}

_COURSE_TOPICS: dict[int, dict] = {
    128781: _TOPICS_STATS,
    130417: _TOPICS_COMP_ARCH,
    129492: _TOPICS_PARADIGMS,
    145184: _TOPICS_NLP,
}


def topics_for_course(cid: int) -> dict:
    return _COURSE_TOPICS.get(int(cid), _TOPICS_STATS)


def detect_course_id(course_dir: Path) -> int | None:
    """Sniff Canvas cid from `<cid>_<slug>` directory name."""
    name = course_dir.name
    for part in name.split("_"):
        if part.isdigit():
            return int(part)
    return None


# Default to STATS for backwards-compat code paths
TOPICS = _TOPICS_STATS


def load_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(errors="ignore")


def load_with_ocr(course_dir: Path, name: str) -> str:
    """Load bundle text but substitute OCR cache where available for cleaner topic counting."""
    md = load_text(course_dir / "bundles" / f"{name}.md")
    ocr_dir = course_dir / "_ocr"
    if not ocr_dir.exists():
        return md
    extra = "\n".join(p.read_text(errors="ignore") for p in ocr_dir.glob("*.txt"))
    return md + "\n" + extra


def count_topics(text: str) -> Counter:
    c: Counter = Counter()
    for topic, aliases in TOPICS.items():
        total = 0
        for pat in aliases:
            total += len(re.findall(pat, text, flags=re.I))
        c[topic] = total
    return c


def normalize(c: Counter) -> dict[str, float]:
    total = sum(c.values()) or 1
    return {k: v / total for k, v in c.items()}


def analyze(course_dir: Path) -> None:
    global TOPICS
    TOPICS = topics_for_course(detect_course_id(course_dir) or 0)
    bundles = course_dir / "bundles"
    if not bundles.exists():
        print(f"Run bundle.py first; missing {bundles}")
        return

    exam_text = "\n".join([
        load_text(bundles / "exams.md"),
        load_text(bundles / "exam_solutions.md"),
        load_text(bundles / "practice.md"),
    ])
    prep_text = "\n".join([
        load_text(bundles / "homeworks.md"),
        load_text(bundles / "hw_keys.md"),
        load_text(bundles / "in_class.md"),
    ])

    exam_counts = count_topics(exam_text)
    prep_counts = count_topics(prep_text)
    exam_norm = normalize(exam_counts)
    prep_norm = normalize(prep_counts)

    rows = []
    for t in TOPICS:
        gap = exam_norm[t] - prep_norm[t]
        ratio = exam_norm[t] / (prep_norm[t] + 1e-9)
        rows.append({
            "topic": t,
            "exam_hits": exam_counts[t],
            "prep_hits": prep_counts[t],
            "exam_share_pct": round(exam_norm[t] * 100, 2),
            "prep_share_pct": round(prep_norm[t] * 100, 2),
            "share_gap_pp": round(gap * 100, 2),
            "exam_to_prep_ratio": round(ratio, 2),
        })

    rows.sort(key=lambda r: r["share_gap_pp"], reverse=True)

    out = course_dir / "bundles" / "topic_gap_report.md"
    with out.open("w") as fh:
        fh.write("# Topic-frequency gap: exams vs prep material\n\n")
        fh.write("Topics ranked by share of exam mentions minus share of prep mentions.\n")
        fh.write("Positive gap = appears more in exams/practice than in HW/in-class. Study these.\n\n")
        fh.write("| topic | exam hits | prep hits | exam % | prep % | gap (pp) | exam/prep ratio |\n")
        fh.write("|---|---:|---:|---:|---:|---:|---:|\n")
        for r in rows:
            fh.write(f"| {r['topic']} | {r['exam_hits']} | {r['prep_hits']} | {r['exam_share_pct']} | {r['prep_share_pct']} | {r['share_gap_pp']} | {r['exam_to_prep_ratio']} |\n")
        fh.write("\n## Top under-prepared topics (positive gap, exam_hits>=3)\n\n")
        for r in rows:
            if r["share_gap_pp"] > 0 and r["exam_hits"] >= 3:
                fh.write(f"- **{r['topic']}** — {r['exam_hits']} exam mentions vs {r['prep_hits']} prep mentions (gap {r['share_gap_pp']}pp)\n")

    (course_dir / "bundles" / "topic_gap.json").write_text(json.dumps(rows, indent=2))
    print(f"Wrote {out}")
    print("\nTop 10 under-prepared:")
    shown = 0
    for r in rows:
        if r["exam_hits"] >= 3:
            print(f"  {r['topic']:30}  exam={r['exam_hits']:4}  prep={r['prep_hits']:4}  gap={r['share_gap_pp']:+.2f}pp")
            shown += 1
            if shown >= 10:
                break


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--course-dir", required=True)
    args = ap.parse_args()
    course_dir = Path(args.course_dir)
    if not course_dir.is_dir():
        print(f"Not a directory: {course_dir}")
        return 1
    analyze(course_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
