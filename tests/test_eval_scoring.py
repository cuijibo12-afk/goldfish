import copy
import math
import unittest

from evals.scoring import score_run


class EvalScoringTests(unittest.TestCase):
    def setUp(self):
        self.corpus = {
            "sessions": [{"messages": [{"id": "m1"}, {"id": "m2"}, {"id": "m3"}, {"id": "m4"}]}],
            "gold_facts": [
                {"fact_id": "f1", "label": "工地", "evidence_ids": ["m1"],
                 "required_detail_groups": [["建筑工地", "施工现场"], ["2025", "二零二五"]]},
                {"fact_id": "f2", "label": "考试", "evidence_ids": ["m2"],
                 "required_detail_groups": [["公务员", "公考"]]},
                {"fact_id": "f3", "label": "咖啡", "evidence_ids": ["m3"],
                 "required_detail_groups": [["咖啡"]]},
            ],
            "gold_profiles": [{"profile_id": "p1", "kind": "interaction_expectation", "evidence_ids": ["m4"],
                               "required_detail_groups": [["自然"], ["交流", "沟通"]]}],
            "gold_relations": [{"relation_id": "r1", "from_fact_id": "f1", "to_fact_id": "f2",
                                "allowed_kinds": ["chronological", "contextual"], "evidence_ids": ["m1", "m2"],
                                "required_reason_groups": [["先后", "时间顺序"]]}],
            "forbidden_relations": [{"relation_id": "bad-r", "from_fact_id": "f1", "to_fact_id": "f2",
                                     "kinds": ["causal_confirmed"], "reason": "没有已确认因果"}],
            "queries": [{"query_id": "q1", "expected_fact_ids": ["f1"], "allowed_fact_ids": ["f2"],
                         "forbidden_fact_ids": ["f3"], "expected_profile_ids": ["p1"], "tags": ["work"]}],
        }
        self.summaries = [
            {"id": "s1", "text": "2025年在施工现场工作", "evidence_ids": ["m1"]},
            {"id": "s2", "text": "准备公考", "evidence_ids": ["m2"]},
            {"id": "s3", "text": "喜欢咖啡", "evidence_ids": ["m3"]},
        ]
        self.profiles = [{"id": "p-db", "kind": "interaction_expectation", "text": "希望自然沟通", "evidence_ids": '["m4"]'}]

    def score(self, summaries=None, profiles=None, links=None, recalls=None):
        return score_run(self.corpus, self.summaries if summaries is None else summaries,
                         self.profiles if profiles is None else profiles,
                         [] if links is None else links,
                         {"q1": {"results": []}} if recalls is None else recalls)

    def test_phrase_alternatives_and_best_detail_probe(self):
        result = self.score()
        self.assertEqual(result["extraction"]["probe_matched_count"], 3)
        self.assertEqual(result["extraction"]["detail_probe_coverage"], 1)
        self.assertFalse(result["methodology"]["semantic_quality_grade"])

    def test_evidence_backed_paraphrase_is_review_not_false_positive(self):
        summaries = [{"id": "s1", "text": "前年在盖房子的地方干活", "evidence_ids": ["m1"]}]
        result = self.score(summaries=summaries, recalls={"q1": {"results": [{"id": "s1"}]}})
        extraction = result["extraction"]
        self.assertEqual(extraction["manual_review_ids"], ["f1"])
        self.assertEqual(extraction["no_evidence_candidate_ids"], ["f2", "f3"])
        query = result["recall"]["queries"][0]
        self.assertEqual(query["unknown_count"], 1)
        self.assertEqual(query["false_positive_count"], 0)
        self.assertIsNone(query["precision"])
        self.assertEqual(query["precision_lower_bound"], 0)
        self.assertEqual(query["precision_upper_bound"], 1)
        self.assertEqual(query["recall"], 0)
        self.assertEqual(query["recall_upper_bound"], 1)

    def test_duplicate_returns_and_allowed_facts_do_not_inflate_recall(self):
        result = self.score(recalls={"q1": {"results": [{"id": "s2"}, {"id": "s2"}]}})
        query = result["recall"]["queries"][0]
        self.assertEqual(query["returned_count"], 1)
        self.assertEqual(query["precision"], 1)
        self.assertEqual(query["recall"], 0)
        self.assertEqual(query["missed_fact_ids"], ["f1"])

    def test_many_facts_per_summary_and_forbidden_mixed_item(self):
        mixed = [{"id": "mixed", "text": "2025施工现场、准备公考、喝咖啡", "evidence_ids": ["m1", "m2", "m3"]}]
        query = self.score(summaries=mixed, recalls={"q1": {"results": [{"id": "mixed"}]}})["recall"]["queries"][0]
        self.assertEqual(query["returned_count"], 1)
        self.assertEqual(query["false_positive_count"], 1)
        self.assertEqual(query["precision"], 0)
        self.assertEqual(query["recall"], 1)
        self.assertEqual(query["items"][0]["forbidden_fact_ids"], ["f3"])

    def test_open_world_content_is_unknown_but_no_evidence_is_explicit(self):
        summaries = self.summaries + [{"id": "unlabelled", "text": "另一件真实的事", "evidence_ids": ["m4"]},
                                      {"id": "unsupported", "text": "没有来源的声称", "evidence_ids": []}]
        query = self.score(summaries=summaries, recalls={"q1": {"results": [{"id": "unlabelled"}, {"id": "unsupported"}]}})["recall"]["queries"][0]
        self.assertEqual(query["unknown_ids"], ["unlabelled"])
        self.assertEqual(query["false_positive_ids"], ["unsupported"])
        self.assertEqual(query["precision_upper_bound"], .5)

    def test_missing_query_is_not_counted_as_a_failed_run_and_empty_denominators(self):
        result = self.score(recalls={})
        self.assertEqual(result["recall"]["not_run_query_ids"], ["q1"])
        self.assertEqual(result["recall"]["summary"]["query_count"], 0)
        self.assertIsNone(result["recall"]["summary"]["recall"])
        self.corpus["queries"][0]["expected_fact_ids"] = []
        query = self.score()["recall"]["queries"][0]
        self.assertIsNone(query["precision"])
        self.assertIsNone(query["recall"])

    def test_profile_classification_and_html_injection(self):
        query = self.score(recalls={"q1": {"results": [], "injection": "<goldfish-memory>\n[交互期望] 希望自然沟通\n</goldfish-memory>"}})["recall"]["queries"][0]
        self.assertEqual(query["profiles"]["recall"], 1)
        wrong = copy.deepcopy(self.profiles)
        wrong[0]["kind"] = "ai_observed"
        result = self.score(profiles=wrong, recalls={"q1": {"results": [], "profile_ids": ["p-db"]}})
        self.assertEqual(result["profiles"]["correctly_classified_count"], 0)
        self.assertEqual(result["profiles"]["classification_errors"][0]["actual_kind"], "ai_observed")
        self.assertEqual(result["recall"]["queries"][0]["profiles"]["recall"], 0)
        self.assertEqual(result["recall"]["queries"][0]["profiles"]["recall_upper_bound"], 0)

    def test_duplicate_content_does_not_hide_wrong_classification(self):
        wrong = copy.deepcopy(self.profiles[0])
        wrong.update(id="wrong", kind="ai_observed")
        scores = self.score(profiles=self.profiles + [wrong])["profiles"]
        self.assertEqual(scores["correctly_classified_count"], 1)
        self.assertEqual(scores["classification_decision_count"], 2)
        self.assertEqual(scores["classification_accuracy_on_probe_matches"], .5)

    def test_feedback_implementation_label_is_counted_as_well_as_chinese(self):
        self.corpus['gold_profiles'][0]['kind'] = 'feedback'
        self.profiles[0]['kind'] = 'feedback'
        for label in ('feedback', '反馈'):
            with self.subTest(label=label):
                result = self.score(recalls={'q1': {'results': [], 'injection': f'[{label}] 希望自然沟通'}})
                self.assertEqual(result['recall']['queries'][0]['profiles']['hit_profile_ids'], ['p1'])

    def test_profile_injection_matches_complete_lines_and_keeps_duplicates_ambiguous(self):
        extended = copy.deepcopy(self.profiles[0])
        extended.update(id="extended", text="希望自然沟通，但只在周末")
        result = self.score(profiles=self.profiles + [extended], recalls={"q1": {
            "results": [], "injection": "[交互期望] 希望自然沟通，但只在周末"}})
        items = result["recall"]["queries"][0]["profiles"]["items"]
        self.assertEqual([item["id"] for item in items], ["extended"])
        duplicate = copy.deepcopy(self.profiles[0])
        duplicate["id"] = "duplicate"
        result = self.score(profiles=self.profiles + [duplicate], recalls={"q1": {
            "results": [], "injection": "[交互期望] 希望自然沟通"}})
        profile = result["recall"]["queries"][0]["profiles"]
        self.assertEqual(profile["returned_count"], 1)
        self.assertEqual(profile["unknown_count"], 1)
        self.assertEqual(profile["recall"], 0)
        self.assertEqual(profile["recall_upper_bound"], 1)

    def test_relations_use_sorted_endpoints_and_report_sources_separately(self):
        link = {"id": "l1", "left_id": "s2", "right_id": "s1", "kind": "chronological", "reason": "先后发生",
                "confidence": .7, "evidence_ids": ["m1", "m2"]}
        result = self.score(links=[link])["relations"]
        self.assertEqual(result["probe_matched_gold_ids"], ["r1"])
        self.assertEqual(result["with_evidence_count"], 1)
        link["kind"] = "same_topic"
        result = self.score(links=[link])["relations"]
        self.assertEqual(result["manual_review_link_ids"], ["l1"])
        self.assertEqual(result["explicitly_wrong_link_ids"], [])

    def test_forbidden_relations_and_structural_errors(self):
        links = [{"id": "bad", "left_id": "s1", "right_id": "s2", "kind": "causal_confirmed", "reason": "", "confidence": math.nan,
                  "evidence_ids": ["m1", "m2"], "confirmed": False, "origin": "model_hypothesis"},
                 {"id": "open", "left_id": "s1", "right_id": "s3", "kind": "contextual", "reason": "相关", "confidence": .5,
                  "evidence_ids": ["m1", "m3"]}]
        result = self.score(links=links)["relations"]
        self.assertEqual(result["explicitly_wrong_link_ids"], ["bad"])
        self.assertEqual(result["manual_review_link_ids"], ["open"])
        self.assertEqual(result["records"][0]["structural_issues"], ["missing_reason", "invalid_confidence", "unconfirmed_causal_claim"])
        self.assertIsNone(result["records"][0]["confidence"])

    def test_invalid_evidence_does_not_earn_fact_recall(self):
        summaries = [{"id": "s1", "text": "2025建筑工地", "evidence_ids": ["m1", "does-not-exist"]}]
        result = self.score(summaries=summaries, recalls={"q1": {"results": [{"id": "s1"}]}})
        self.assertEqual(result["extraction"]["invalid_evidence_record_ids"], ["s1"])
        self.assertEqual(result["recall"]["queries"][0]["recall"], 0)

    def test_forbidden_multi_fact_link_cannot_also_increase_gold_coverage(self):
        self.corpus["forbidden_relations"][0]["kinds"] = ["chronological"]
        link = {"id": "l1", "left_id": "s1", "right_id": "s2", "kind": "chronological", "reason": "先后发生",
                "confidence": .7, "evidence_ids": ["m1", "m2"]}
        result = self.score(links=[link])["relations"]
        self.assertEqual(result["explicitly_wrong_link_ids"], ["l1"])
        self.assertEqual(result["probe_matched_gold_ids"], [])

    def test_words_are_probes_and_not_claims_of_semantic_validity(self):
        summaries = [{"id": "s3", "text": "用户不喜欢咖啡", "evidence_ids": ["m3"]}]
        result = self.score(summaries=summaries)
        self.assertEqual(result["extraction"]["probe_matched_ids"], ["f3"])
        self.assertFalse(result["methodology"]["semantic_quality_grade"])


if __name__ == "__main__":
    unittest.main()
