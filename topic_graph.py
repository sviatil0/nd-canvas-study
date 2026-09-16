"""Per-course topic dependency graphs.

Each course has its own graph keyed by Canvas course id. Backwards-compatible:
the global `GRAPH` symbol defaults to ACMS 30440 (statistics) so existing code
keeps working. To use a different course's graph, call `for_course(cid)`.
"""
from __future__ import annotations

# ACMS 30440 — Statistics (cid 128781)
_STATS: dict[str, dict] = {
    "descriptive_statistics":   {"ch": 1,  "label": "Descriptive Statistics",            "prereqs": []},
    "probability_basics":       {"ch": 2,  "label": "Probability Basics",                "prereqs": ["descriptive_statistics"]},
    "conditional_probability":  {"ch": 2,  "label": "Conditional Probability",           "prereqs": ["probability_basics"]},
    "independence":             {"ch": 2,  "label": "Independence",                      "prereqs": ["conditional_probability"]},
    "discrete_distributions":   {"ch": 3,  "label": "Discrete Distributions",            "prereqs": ["probability_basics", "independence"]},
    "expected_value":           {"ch": 3,  "label": "Expected Value & Variance",         "prereqs": ["discrete_distributions"]},
    "continuous_distributions": {"ch": 4,  "label": "Continuous Distributions",          "prereqs": ["expected_value"]},
    "joint_distributions":      {"ch": 5,  "label": "Joint Distributions",               "prereqs": ["discrete_distributions", "continuous_distributions"]},
    "point_estimation":         {"ch": 6,  "label": "Point Estimation (MLE, MoM)",       "prereqs": ["joint_distributions"]},
    "sampling_distributions":   {"ch": 6,  "label": "Sampling Distributions & CLT",      "prereqs": ["point_estimation", "continuous_distributions"]},
    "confidence_intervals":     {"ch": 7,  "label": "Confidence Intervals (1 sample)",   "prereqs": ["sampling_distributions"]},
    "hypothesis_testing":       {"ch": 8,  "label": "Hypothesis Testing (1 sample)",     "prereqs": ["confidence_intervals"]},
    "z_test":                   {"ch": 8,  "label": "Z-test",                            "prereqs": ["hypothesis_testing"]},
    "t_test":                   {"ch": 8,  "label": "t-test",                            "prereqs": ["hypothesis_testing"]},
    "two_sample":               {"ch": 9,  "label": "Two-sample Inference",              "prereqs": ["t_test", "z_test"]},
    "anova":                    {"ch": 10, "label": "ANOVA + Tukey HSD",                 "prereqs": ["two_sample"]},
    "regression_simple":        {"ch": 12, "label": "Simple Linear Regression",          "prereqs": ["hypothesis_testing", "expected_value"]},
    "correlation":              {"ch": 12, "label": "Correlation",                       "prereqs": ["regression_simple"]},
    "regression_multiple":      {"ch": 13, "label": "Multiple Regression",               "prereqs": ["regression_simple"]},
    "regression_assumptions":   {"ch": 13, "label": "Regression Assumptions / Diagnostics", "prereqs": ["regression_multiple"]},
    "categorical_data":         {"ch": 14, "label": "Categorical Data Analysis",         "prereqs": ["hypothesis_testing"]},
    "chi_squared":              {"ch": 14, "label": "Chi-Squared Tests",                 "prereqs": ["categorical_data"]},
    "nonparametric":            {"ch": 14, "label": "Nonparametric Methods",             "prereqs": ["hypothesis_testing"]},
}

# CSE 30321 — Computer Architecture (cid 130417)
# "ch" = lecture-block index for sequential ordering on the study path.
_COMP_ARCH: dict[str, dict] = {
    "isa_basics":               {"ch": 1,  "label": "ISA & Processor Review",                       "prereqs": []},
    "performance_metrics":      {"ch": 2,  "label": "Performance (CPI, Amdahl's Law, MIPS)",        "prereqs": ["isa_basics"]},
    "riscv_instructions":       {"ch": 3,  "label": "RISC-V Instructions & Programming",            "prereqs": ["isa_basics"]},
    "riscv_encoding":           {"ch": 4,  "label": "RISC-V Instruction Types & Encodings",         "prereqs": ["riscv_instructions"]},
    "riscv_control_procedures": {"ch": 5,  "label": "RISC-V Control Flow & Procedures",             "prereqs": ["riscv_encoding"]},
    "pipelining_intro":         {"ch": 6,  "label": "Pipelining Introduction",                      "prereqs": ["riscv_control_procedures", "performance_metrics"]},
    "pipeline_data_hazards":    {"ch": 7,  "label": "Pipeline Data Hazards & Forwarding",           "prereqs": ["pipelining_intro"]},
    "pipeline_control_hazards": {"ch": 8,  "label": "Pipeline Control Hazards",                     "prereqs": ["pipeline_data_hazards"]},
    "memory_hierarchy_intro":   {"ch": 9,  "label": "Memory Hierarchy Introduction",                "prereqs": ["pipeline_control_hazards"]},
    "direct_mapped_cache":      {"ch": 10, "label": "Direct-Mapped Caches",                         "prereqs": ["memory_hierarchy_intro"]},
    "associative_cache":        {"ch": 11, "label": "Associative Caches",                           "prereqs": ["direct_mapped_cache"]},
    "cache_performance":        {"ch": 12, "label": "Cache Performance (AMAT, miss rate)",          "prereqs": ["associative_cache"]},
    "cache_aware_programming":  {"ch": 13, "label": "Cache-Aware Programming",                      "prereqs": ["cache_performance"]},
    "virtual_memory_intro":     {"ch": 14, "label": "Virtual Memory Introduction",                  "prereqs": ["cache_performance"]},
    "vm_paging":                {"ch": 15, "label": "VM Paging & Page Tables",                      "prereqs": ["virtual_memory_intro"]},
    "vm_tlb_performance":       {"ch": 16, "label": "VM Performance (TLB)",                         "prereqs": ["vm_paging"]},
    "branch_prediction":        {"ch": 17, "label": "Branch Prediction",                            "prereqs": ["pipeline_control_hazards"]},
    "scheduling_intro":         {"ch": 18, "label": "Instruction Scheduling Introduction",          "prereqs": ["pipeline_data_hazards"]},
    "dynamic_scheduling":       {"ch": 19, "label": "Dynamic Scheduling (Tomasulo, ROB)",           "prereqs": ["scheduling_intro", "branch_prediction"]},
    "parallelism_intro":        {"ch": 20, "label": "Parallelism Introduction",                     "prereqs": ["dynamic_scheduling"]},
    "cache_coherence":          {"ch": 21, "label": "Cache Coherence (MESI, snooping)",             "prereqs": ["parallelism_intro", "cache_performance"]},
}

# CSE 30332 — Programming Paradigms (cid 129492)
_PARADIGMS: dict[str, dict] = {
    "course_overview":          {"ch": 1,  "label": "Course Overview & Paradigm Intro",                                 "prereqs": []},
    "javascript_basics":        {"ch": 2,  "label": "JavaScript Basics (types, vars, functions, hoisting)",             "prereqs": ["course_overview"]},
    "javascript_closures":      {"ch": 3,  "label": "JavaScript Closures, Scope & this",                                 "prereqs": ["javascript_basics"]},
    "javascript_objects":       {"ch": 4,  "label": "JavaScript Objects, Prototypes & Classes",                         "prereqs": ["javascript_closures"]},
    "javascript_async":         {"ch": 5,  "label": "JavaScript Async / Event Loop / Promises / Callbacks",             "prereqs": ["javascript_objects"]},
    "frontend_dom":             {"ch": 6,  "label": "Front-end: DOM, Events, Event-driven Programming",                 "prereqs": ["javascript_async"]},
    "frontend_mvc":             {"ch": 7,  "label": "Front-end: MVC pattern in browser",                                "prereqs": ["frontend_dom"]},
    "exam1_review":             {"ch": 8,  "label": "Exam 1 Review (JS + Front-end)",                                   "prereqs": ["frontend_mvc"]},
    "python_basics":            {"ch": 9,  "label": "Python Basics & Differences from JS (typing, scope, mutability)",  "prereqs": ["exam1_review"]},
    "python_advanced":          {"ch": 10, "label": "Python Advanced (decorators, comprehensions, OOP)",                "prereqs": ["python_basics"]},
    "django_intro":             {"ch": 11, "label": "Django Intro (project structure, MVT vs MVC)",                     "prereqs": ["python_advanced"]},
    "django_models":            {"ch": 12, "label": "Django Models & ORM",                                              "prereqs": ["django_intro"]},
    "django_views_templates":   {"ch": 13, "label": "Django Views & Templates",                                         "prereqs": ["django_models"]},
    "django_forms_auth":        {"ch": 14, "label": "Django Forms, Auth & Sessions",                                    "prereqs": ["django_views_templates"]},
    "rest_apis":                {"ch": 15, "label": "REST APIs (HTTP verbs, statelessness, resources)",                 "prereqs": ["django_views_templates"]},
    "exam2_review":             {"ch": 16, "label": "Exam 2 Review (Python + Django + REST)",                           "prereqs": ["rest_apis"]},
    "java_basics":              {"ch": 17, "label": "Java Basics (static typing, JVM, class structure)",                "prereqs": ["exam2_review"]},
    "java_oop":                 {"ch": 18, "label": "Java OOP (inheritance, interfaces, polymorphism, late binding)",   "prereqs": ["java_basics"]},
    "java_collections":         {"ch": 19, "label": "Java Collections, Generics, equals/hashCode",                      "prereqs": ["java_oop"]},
    "java_concurrency":         {"ch": 20, "label": "Java Concurrency (threads, java.util.concurrent, sync)",           "prereqs": ["java_collections"]},
    "clojure_intro":            {"ch": 21, "label": "Clojure Intro & Functional Paradigm",                              "prereqs": ["java_oop"]},
    "clojure_immutability":     {"ch": 22, "label": "Clojure Immutability, Recursion, Lazy Evaluation",                 "prereqs": ["clojure_intro"]},
    "functional_higher_order":  {"ch": 23, "label": "Higher-order functions: map, filter, reduce (cross-language)",     "prereqs": ["javascript_basics", "clojure_immutability"]},
    "paradigms_comparison":     {"ch": 24, "label": "Paradigm Comparison (imperative, OOP, functional, declarative)",   "prereqs": ["functional_higher_order", "java_oop", "clojure_immutability"]},
    "binding_typing":           {"ch": 25, "label": "Binding & Typing (deep/shallow, static/dynamic, strong/weak)",     "prereqs": ["javascript_closures", "python_basics", "java_basics"]},
}

# CSE 40657 — Natural Language Processing (cid 145184)
# "ch" = week index on the instructor's fall-2026 schedule.
_NLP: dict[str, dict] = {
    "nlp_overview":                {"ch": 1,  "label": "NLP Overview & Syllabus",              "prereqs": [], "readings": ['readings/SLP3_chapter1.pdf']},
    "ml_foundations":              {"ch": 1,  "label": "NLP Tasks & ML Foundations",           "prereqs": ["nlp_overview"], "readings": ['readings/SLP3_chapter4.pdf', 'readings/SLP3_chapterB.pdf']},
    "tokenization":                {"ch": 1,  "label": "Words & Tokenization (SLP C2)",        "prereqs": ["nlp_overview"], "readings": ['readings/SLP3_chapter2_*.pdf']},
    "ngram_language_models":       {"ch": 2,  "label": "N-gram Language Models (SLP C3)",      "prereqs": ["tokenization", "ml_foundations"], "readings": ['readings/SLP3_chapter3_*.pdf', 'readings/SLP3_chapterC.pdf']},
    "lm_evaluation":               {"ch": 2,  "label": "Evaluation & Perplexity",              "prereqs": ["ngram_language_models"], "readings": ['readings/SLP3_chapter3_*.pdf']},
    "neural_networks":             {"ch": 2,  "label": "Neural Networks (PyTorch)",            "prereqs": ["ml_foundations"], "readings": ['readings/SLP3_chapter6.pdf']},
    "neural_language_models":      {"ch": 3,  "label": "Neural Language Models & Embeddings",  "prereqs": ["neural_networks", "ngram_language_models"], "readings": ['readings/SLP3_chapter5.pdf', 'readings/SLP3_chapter14.pdf']},
    "pos_tagging_parsing":         {"ch": 4,  "label": "POS Tagging & Parsing",                "prereqs": ["neural_language_models"], "readings": ['readings/SLP3_chapter18.pdf', 'readings/SLP3_chapter19.pdf', 'readings/SLP3_chapter20.pdf', 'readings/SLP3_chapterA.pdf']},
    "encoder_decoder_attention":   {"ch": 5,  "label": "Encoder-Decoder & Attention",          "prereqs": ["neural_language_models"], "readings": ['readings/SLP3_chapter14.pdf', 'readings/SLP3_chapter7.pdf']},
    "machine_translation":         {"ch": 6,  "label": "Machine Translation",                  "prereqs": ["encoder_decoder_attention", "lm_evaluation"], "readings": ['readings/SLP3_chapter13.pdf']},
    "transformers":                {"ch": 7,  "label": "Transformer Language Models",          "prereqs": ["encoder_decoder_attention"], "readings": ['readings/SLP3_chapter7.pdf', 'readings/SLP3_chapter9.pdf']},
    "llm_training":                {"ch": 9,  "label": "Training Large Language Models",       "prereqs": ["transformers"], "readings": ['readings/SLP3_chapter7.pdf']},
    "llm_posttraining":            {"ch": 10, "label": "LLM Post-training (RLHF, tuning)",     "prereqs": ["llm_training"], "readings": ['readings/SLP3_chapter8.pdf']},
    "semantics_retrieval":         {"ch": 11, "label": "Applied Semantics & Retrieval",        "prereqs": ["transformers"], "readings": ['readings/SLP3_chapter11.pdf', 'readings/SLP3_chapter22.pdf', 'readings/SLP3_chapterI.pdf']},
    "evaluation_interpretability": {"ch": 12, "label": "Evaluation & Interpretability",        "prereqs": ["llm_posttraining", "lm_evaluation"], "readings": ['readings/SLP3_chapter10.pdf']},
    "responsible_nlp":             {"ch": 13, "label": "Responsible NLP",                      "prereqs": ["evaluation_interpretability"], "readings": ['readings/SLP3_chapter23.pdf', 'readings/SLP3_chapter10.pdf']},
    "multilingual_lowresource":    {"ch": 14, "label": "Multilingual & Low-resource NLP",      "prereqs": ["transformers", "machine_translation"], "readings": ['readings/SLP3_chapter13.pdf']},
}

# CSE 30264 — Computer Networks FA26 (cid 145183), Kurose/Ross chapter numbers.
# Covers lectures delivered so far; extend as the course advances.
_NETWORKS: dict[str, dict] = {
    "internet_overview":        {"ch": 1, "label": "What is the Internet? Protocols, hosts, RFCs",          "prereqs": []},
    "network_edge":             {"ch": 1, "label": "Network Edge: access technologies & physical media",    "prereqs": ["internet_overview"]},
    "packet_circuit_switching": {"ch": 1, "label": "Packet Switching vs Circuit Switching",                 "prereqs": ["internet_overview"]},
    "delay_types":              {"ch": 1, "label": "Delay: processing, queuing, transmission, propagation", "prereqs": ["packet_circuit_switching"]},
    "throughput":               {"ch": 1, "label": "Throughput & bottleneck links",                         "prereqs": ["delay_types"]},
    "protocol_layering":        {"ch": 1, "label": "Protocol Layers & Encapsulation (Internet stack)",      "prereqs": ["internet_overview"]},
    "internet_history":         {"ch": 1, "label": "History of the Internet: telephony to ARPANET to today","prereqs": ["packet_circuit_switching"]},
    "network_security_intro":   {"ch": 1, "label": "Networks Under Attack: malware, DoS/DDoS, sniffing",    "prereqs": ["protocol_layering"]},
}

# Map Canvas course id → graph
COURSE_GRAPHS: dict[int, dict[str, dict]] = {
    128781: _STATS,
    130417: _COMP_ARCH,
    129492: _PARADIGMS,
    145184: _NLP,
    145183: _NETWORKS,
}

# Default: stats (backwards compat for existing code paths)
GRAPH: dict[str, dict] = _STATS


def for_course(cid: int) -> dict[str, dict]:
    """Return the graph for a given Canvas course id, or empty dict if unknown."""
    return COURSE_GRAPHS.get(int(cid), {})


def topo_sort(graph: dict[str, dict] | None = None) -> list[str]:
    """Kahn's algorithm — topics in dependency order."""
    g = graph if graph is not None else GRAPH
    indeg = {k: 0 for k in g}
    for k, v in g.items():
        for p in v["prereqs"]:
            if p in g:
                indeg[k] += 1
    ready = [k for k, d in indeg.items() if d == 0]
    out: list[str] = []
    while ready:
        ready.sort(key=lambda k: (g[k]["ch"], k))
        n = ready.pop(0)
        out.append(n)
        for k, v in g.items():
            if n in v["prereqs"]:
                indeg[k] -= 1
                if indeg[k] == 0:
                    ready.append(k)
    return out


def mermaid(graph: dict[str, dict] | None = None) -> str:
    g = graph if graph is not None else GRAPH
    lines = ["flowchart LR"]
    seen = set()
    for k, v in g.items():
        label = f'{k}["Ch{v["ch"]}: {v["label"]}"]'
        if k not in seen:
            lines.append(f"  {label}")
            seen.add(k)
    for k, v in g.items():
        for p in v["prereqs"]:
            if p in g:
                lines.append(f"  {p} --> {k}")
    return "\n".join(lines)


def prereqs_of(topic: str, graph: dict[str, dict] | None = None) -> list[str]:
    g = graph if graph is not None else GRAPH
    return g.get(topic, {}).get("prereqs", [])


def dependents_of(topic: str, graph: dict[str, dict] | None = None) -> list[str]:
    g = graph if graph is not None else GRAPH
    return [k for k, v in g.items() if topic in v["prereqs"]]
