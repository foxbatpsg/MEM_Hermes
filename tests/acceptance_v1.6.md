# Отчёт о приёмке MiniMem-1

Файл формируется автоматически: `python -B tools/build_acceptance_report.py`.
Номера берутся из `tests/required_tests.txt`, статусы — из фактического
прогона тестов и чтения docstring проверок.

Сформирован: 2026-09-26T18:59:55+04:00

## Итог прогона

- тестов выполнено: 348;
- успешно: 345;
- пропущено: 3 (установочные тесты Hermes).

## Статусы номеров

| Номер | Статус | Проверка |
|---|---|---|
| 1 | пройден | test_capture.py::test_1_one_turn_creates_one_record |
| 2 | пройден | test_capture.py::test_2_same_turn_delivered_twice_creates_one_record |
| 3 | пройден | test_revision_numbers.py::test_3_event_id_stable_while_content_hash_changes |
| 4 | пройден | test_capture.py::test_4_secrets_removed_before_write |
| 5 | пройден | test_capture.py::test_5_6_project_resolved_and_fallback |
| 6 | пройден | test_capture.py::test_5_6_project_resolved_and_fallback |
| 7 | пройден | test_capture.py::test_7_identical_turns_get_distinct_event_ids_same_hash |
| 8 | пройден | test_capture.py::test_8_truncation_flag_written |
| 9 | пройден | test_indexer.py::test_9_new_record_appears_in_fts_and_meta |
| 10 | пройден | test_indexer.py::test_10_indexer_error_does_not_delete_journal_record |
| 11 | пройден | test_indexer.py::test_11_rebuild_after_deleting_db_restores_index |
| 12 | пройден | test_indexer.py::test_12_search_does_not_see_other_project, test_search.py::test_12_other_project_is_not_returned |
| 13 | пройден | test_capture.py::test_13_markers_and_markup_do_not_break_parsing |
| 14 | пройден | test_capture.py::test_14_journal_is_valid_markdown |
| 15 | пройден | test_indexer.py::test_15_skipped_record_is_added_on_next_run |
| 16 | пройден | test_indexer.py::test_16_verify_finds_journal_record_missing_in_index, test_search.py::test_16_usage_counted_only_for_inserted_records |
| 17 | пройден | test_ret.py::test_17_return_inserts_previous_session_digest, test_search.py::test_17_returned_records_are_marked_in_session_state |
| 18 | пройден | test_ret.py::test_18_return_current_session_digest_after_compaction |
| 19 | пройден | test_ret.py::test_19_return_does_not_run_fts_search |
| 20 | пройден | test_ret.py::test_20_return_does_not_call_model |
| 21 | пройден | test_ret.py::test_21_return_size_within_max_return_chars |
| 22 | пройден | test_search.py::test_22_substantive_turn_finds_relevant_records, test_search.py::test_46_hook_outputs_search_context_json |
| 23 | пройден | test_search.py::test_23_short_service_turn_does_not_search |
| 24 | пройден | test_search.py::test_24_already_returned_record_is_not_returned_again |
| 25 | пройден | test_search.py::test_25_suppressed_record_is_not_returned |
| 26 | пройден | test_search.py::test_26_result_limited_by_count_and_size |
| 27 | пройден | test_search.py::test_27_user_text_cannot_change_query_structure |
| 28 | пройден | test_search.py::test_28_search_is_skipped_on_first_turn |
| 29 | пройден | test_search.py::test_29_returned_record_contains_date_project_and_source |
| 30 | пройден | test_ret.py::test_30_selection_uses_last_turn_and_excludes_current_session, test_ret.py::test_30_tie_break_prefers_smaller_file_name |
| 31 | пройден | test_digest.py::test_31_rebuild_restores_file_from_journal |
| 32 | пройден | test_digest.py::test_32_suppressed_record_not_in_digest |
| 33 | пройден | test_digest.py::test_33_size_within_max_digest_chars |
| 34 | пройден | test_compaction.py::test_34_journal_records_are_not_deleted |
| 35 | пройден | test_compaction.py::test_35_suppressed_records_stored_in_service_layer |
| 36 | пройден | test_compaction.py::test_36_compaction_does_not_call_model_or_read_journal |
| 37 | пройден | test_compaction.py::test_37_rules_are_deterministic |
| 38 | пройден | test_compaction.py::test_38_second_of_two_duplicates_is_suppressed |
| 39 | пройден | test_compaction.py::test_39_rule2_does_not_apply_to_records_before_rebuild |
| 40 | пройден | test_stage7.py::test_40_catch_up_failure_does_not_break_turn, test_stage7.py::test_40_compaction_failure_does_not_break_turn, test_stage7.py::test_40_digest_failure_does_not_break_turn, test_stage7.py::test_40_session_end_failure_keeps_hook_exit_zero, test_stage7.py::test_40_pre_llm_hook_exits_zero_on_broken_modules |
| 41 | пройден | test_stage7.py::test_41_pre_llm_hook_fits_timeout |
| 42 | пройден | test_indexer.py::test_11_rebuild_after_deleting_db_restores_index |
| 43 | пройден | test_compaction.py::test_43_same_content_in_two_projects_is_not_duplicate, test_indexer.py::test_43_same_content_in_two_projects_is_not_duplicate |
| 44 | пройден | test_compaction.py::test_44_rebuild_restores_rules_1_and_3_only |
| 45 | пройден | test_ret.py::test_45_decision_uses_thresholds_and_is_logged |
| 46 | пройден | test_ret.py::test_46_empty_output_does_not_change_user_message, test_ret.py::test_46_broken_input_and_bad_config_exit_zero, test_search.py::test_46_empty_search_output_keeps_exit_zero |
| 47 | пройден | test_stage8.py::InstallationTest47 (hermes_home, test_all_hooks_registered, test_commands_allowlisted, test_hermes_hooks_doctor_reports_no_errors) |
| 48 | пройден | test_search.py::test_48_word_form_variant_is_found |
| 49 | пройден | test_search.py::test_49_short_word_does_not_enter_query, test_search.py::test_49_min_word_len_is_configurable |
| 50 | пройден | test_search.py::test_50_stem_prefix_is_not_substring_search, test_search.py::test_50_terms_are_limited_and_unique |
| 51 | пройден | test_capture.py::IdempotencyTests (setUp, test_2_same_turn_delivered_twice_creates_one_record, test_61_conflicting_redelivery_keeps_single_canonical_record, test_62_conflict_is_logged, test_162_both_versions_stay_in_journal, test_51_capture_disabled_writes_nothing, test_114_disabling_capture_keeps_existing_journal, test_102_capture_survives_unavailable_sqlite), test_capture.py::test_51_capture_disabled_writes_nothing |
| 52 | пройден | test_stage7.py::SessionEndLifecycleTests (prepare_session, test_52_digest_created_on_session_end_with_mode_return_false, test_session_end_runs_compaction_before_digest, test_session_end_compaction_disabled_keeps_digest, test_interrupted_session_end_writes_completion, test_158_interrupted_digest_carries_warning_on_insert, test_session_end_hook_process_exits_zero_with_empty_output), test_stage7.py::test_52_digest_created_on_session_end_with_mode_return_false |
| 53 | пройден | test_search.py::test_mode_search_false_inserts_nothing |
| 54 | пройден | test_compaction.py::test_54_disabled_mode_does_not_change_suppression_state, test_compaction.py::test_cli_compact_runs_with_mode_disabled |
| 55 | пройден | test_stage7.py::ModeSwitchTests (prepare_previous, test_55_switching_modes_needs_no_rebuild, test_55_compaction_mode_toggle_keeps_records, test_111_digest_timeout_leaves_return_without_insert, test_111_digest_created_when_budget_sufficient, test_113_cli_compact_ignores_mode_compaction_flag), test_stage7.py::test_55_switching_modes_needs_no_rebuild, test_stage7.py::test_55_compaction_mode_toggle_keeps_records |
| 56 | пройден | test_revision_numbers.py::test_56_capture_only_mode_writes_journal_without_context |
| 57 | пройден | test_indexer.py::IndexBasicsTests (test_9_new_record_appears_in_fts_and_meta, test_57_sqlite_keeps_all_identifiers, test_11_rebuild_after_deleting_db_restores_index, test_70_cursor_avoids_full_read, test_15_skipped_record_is_added_on_next_run, test_16_verify_finds_journal_record_missing_in_index), test_indexer.py::test_57_sqlite_keeps_all_identifiers |
| 58 | пройден | test_digest.py::DigestSelectionTests (test_58_digest_built_by_session_id_without_full_journal_scan, test_rebuild_does_not_read_journal, test_empty_session_is_not_created, test_damaged_digest_is_reported_not_raised, test_missing_file_reported_by_verify, test_verify_reports_name_collision), test_digest.py::test_58_digest_built_by_session_id_without_full_journal_scan |
| 59 | пройден | test_indexer.py::ProjectFilterTests (setUp, _other_project_turn, test_12_search_does_not_see_other_project, test_43_same_content_in_two_projects_is_not_duplicate, test_59_session_excluded_filter_uses_session_id), test_indexer.py::test_59_session_excluded_filter_uses_session_id |
| 60 | пройден | test_compaction.py::CompactionTieBreakerTests (kept, test_60_first_kept_by_timestamp_turn_and_event_id, test_60_tie_breaker_is_antisymmetric_and_order_independent, test_60_derived_turn_does_not_disturb_order), test_compaction.py::test_60_first_kept_by_timestamp_turn_and_event_id |
| 61 | пройден | test_capture.py::IdempotencyTests (setUp, test_2_same_turn_delivered_twice_creates_one_record, test_61_conflicting_redelivery_keeps_single_canonical_record, test_62_conflict_is_logged, test_162_both_versions_stay_in_journal, test_51_capture_disabled_writes_nothing, test_114_disabling_capture_keeps_existing_journal, test_102_capture_survives_unavailable_sqlite), test_capture.py::test_61_conflicting_redelivery_keeps_single_canonical_record |
| 62 | пройден | test_capture.py::IdempotencyTests (setUp, test_2_same_turn_delivered_twice_creates_one_record, test_61_conflicting_redelivery_keeps_single_canonical_record, test_62_conflict_is_logged, test_162_both_versions_stay_in_journal, test_51_capture_disabled_writes_nothing, test_114_disabling_capture_keeps_existing_journal, test_102_capture_survives_unavailable_sqlite), test_capture.py::test_62_conflict_is_logged, test_indexer.py::DuplicateAndRevisionTests (_append_physical_duplicate, test_103_physical_duplicate_handled_deterministically, test_115_rebuild_after_duplicate_is_deterministic, test_104_verify_reports_duplicate_physical_records, test_162_revision_replaces_index_entry, test_163_verify_reports_more_than_one_revision, test_64_index_keeps_journal_content_hash) |
| 63 | пройден | test_capture.py::SizeLimitTests (setUp, test_105_record_respects_max_memory_record, test_105_metadata_is_never_dropped, test_metadata_survives_truncation, test_63_hash_uses_full_content_not_truncated, test_64_stored_hash_is_not_recomputed_from_body), test_capture.py::test_63_hash_uses_full_content_not_truncated |
| 64 | пройден | test_capture.py::SizeLimitTests (setUp, test_105_record_respects_max_memory_record, test_105_metadata_is_never_dropped, test_metadata_survives_truncation, test_63_hash_uses_full_content_not_truncated, test_64_stored_hash_is_not_recomputed_from_body), test_capture.py::test_64_stored_hash_is_not_recomputed_from_body, test_compaction.py::test_64_content_hash_not_recomputed_from_truncated_text, test_indexer.py::test_64_index_keeps_journal_content_hash |
| 65 | пройден | test_compaction.py::test_65_rule3_keeps_records_with_different_hash |
| 66 | пройден | test_capture.py::DamagedRecordTests (setUp, write, good_record, test_66_partial_record_keeps_next_valid_record, test_67_nested_begin_marks_previous_damaged, test_69_damaged_record_does_not_stop_others, test_148_partial_append_is_reported_by_size_check, test_100_unknown_metadata_key_is_not_damage, test_100_invalid_enum_is_metadata_error), test_capture.py::test_66_partial_record_keeps_next_valid_record |
| 67 | пройден | test_capture.py::DamagedRecordTests (setUp, write, good_record, test_66_partial_record_keeps_next_valid_record, test_67_nested_begin_marks_previous_damaged, test_69_damaged_record_does_not_stop_others, test_148_partial_append_is_reported_by_size_check, test_100_unknown_metadata_key_is_not_damage, test_100_invalid_enum_is_metadata_error), test_capture.py::test_67_nested_begin_marks_previous_damaged |
| 68 | пройден | test_capture.py::JournalWriteTests (setUp, sample, test_records_separated_by_blank_line, test_offsets_point_to_record_start, test_68_writes_do_not_mix, test_149_damaged_key_is_stable_across_reads, test_body_escaping_roundtrip), test_capture.py::test_68_writes_do_not_mix |
| 69 | пройден, не обязателен для первой приёмки (§23) | test_capture.py::DamagedRecordTests (setUp, write, good_record, test_66_partial_record_keeps_next_valid_record, test_67_nested_begin_marks_previous_damaged, test_69_damaged_record_does_not_stop_others, test_148_partial_append_is_reported_by_size_check, test_100_unknown_metadata_key_is_not_damage, test_100_invalid_enum_is_metadata_error), test_capture.py::test_69_damaged_record_does_not_stop_others |
| 70 | пройден, не обязателен для первой приёмки (§23) | test_indexer.py::IndexBasicsTests (test_9_new_record_appears_in_fts_and_meta, test_57_sqlite_keeps_all_identifiers, test_11_rebuild_after_deleting_db_restores_index, test_70_cursor_avoids_full_read, test_15_skipped_record_is_added_on_next_run, test_16_verify_finds_journal_record_missing_in_index), test_indexer.py::test_70_cursor_avoids_full_read |
| 71 | пройден, не обязателен для первой приёмки (§23) | test_indexer.py::CursorTests (test_71_skipped_record_indexed_after_failure, test_72_cursor_invalid_when_file_shrinks, test_149_damaged_record_reported_once_per_file_offset, test_73_batch_limit_stops_pass, test_10_indexer_error_does_not_delete_journal_record), test_indexer.py::test_71_skipped_record_indexed_after_failure |
| 72 | пройден, не обязателен для первой приёмки (§23) | test_indexer.py::CursorTests (test_71_skipped_record_indexed_after_failure, test_72_cursor_invalid_when_file_shrinks, test_149_damaged_record_reported_once_per_file_offset, test_73_batch_limit_stops_pass, test_10_indexer_error_does_not_delete_journal_record), test_indexer.py::test_72_cursor_invalid_when_file_shrinks |
| 73 | пройден, не обязателен для первой приёмки (§23) | test_indexer.py::CursorTests (test_71_skipped_record_indexed_after_failure, test_72_cursor_invalid_when_file_shrinks, test_149_damaged_record_reported_once_per_file_offset, test_73_batch_limit_stops_pass, test_10_indexer_error_does_not_delete_journal_record), test_indexer.py::test_73_batch_limit_stops_pass |
| 74 | пройден, не обязателен для первой приёмки (§23) | test_stage7.py::CatchUpFirstTurnTests (prepare_previous, test_74_catch_up_creates_previous_digest_and_clears_pending, test_74_catch_up_skipped_without_previous_session, test_74_previous_session_without_digests_chosen_by_last_seen, test_110_catch_up_not_repeated_when_digest_done, test_74_interrupted_previous_session_gets_completion_field, test_78_catch_up_without_mode_return_creates_no_digest), test_stage7.py::test_74_catch_up_creates_previous_digest_and_clears_pending, test_stage7.py::test_74_catch_up_skipped_without_previous_session, test_stage7.py::test_74_previous_session_without_digests_chosen_by_last_seen, test_stage7.py::test_74_interrupted_previous_session_gets_completion_field |
| 75 | пройден, не обязателен для первой приёмки (§23) | test_stage7.py::CatchUpBudgetTests (prepare_previous, test_75_budget_exhausted_sets_pending_and_increments_attempts, test_75_retry_succeeds_on_next_turn), test_stage7.py::test_75_budget_exhausted_sets_pending_and_increments_attempts, test_stage7.py::test_75_retry_succeeds_on_next_turn |
| 76 | пройден, не обязателен для первой приёмки (§23) | test_stage7.py::CatchUpIdempotencyTests (prepare_previous, test_76_repeated_catch_up_creates_one_digest, test_76_repeated_catch_up_is_idempotent_for_compaction, test_110_catch_up_skipped_when_digest_valid_and_done, test_110_damaged_digest_is_rebuilt_by_catch_up), test_stage7.py::test_76_repeated_catch_up_creates_one_digest, test_stage7.py::test_76_repeated_catch_up_is_idempotent_for_compaction |
| 77 | пройден, не обязателен для первой приёмки (§23) | test_stage7.py::CatchUpFirstTurnTests (prepare_previous, test_74_catch_up_creates_previous_digest_and_clears_pending, test_74_catch_up_skipped_without_previous_session, test_74_previous_session_without_digests_chosen_by_last_seen, test_110_catch_up_not_repeated_when_digest_done, test_74_interrupted_previous_session_gets_completion_field, test_78_catch_up_without_mode_return_creates_no_digest), test_stage7.py::CatchUpCompactionModeTests (prepare_previous, test_77_catch_up_compacts_previous_session, test_77_catch_up_without_compaction_mode_creates_digest), test_stage7.py::test_77_catch_up_compacts_previous_session, test_stage7.py::test_77_catch_up_without_compaction_mode_creates_digest |
| 78 | пройден, не обязателен для первой приёмки (§23) | test_stage7.py::CatchUpFirstTurnTests (prepare_previous, test_74_catch_up_creates_previous_digest_and_clears_pending, test_74_catch_up_skipped_without_previous_session, test_74_previous_session_without_digests_chosen_by_last_seen, test_110_catch_up_not_repeated_when_digest_done, test_74_interrupted_previous_session_gets_completion_field, test_78_catch_up_without_mode_return_creates_no_digest), test_stage7.py::test_78_catch_up_without_mode_return_creates_no_digest |
| 79 | пройден | test_compaction.py::CompactionRule2Tests (test_79_rule2_disabled_by_default, test_rule2_requires_record_created_after_rebuild, test_rule2_skips_used_records, test_39_rule2_does_not_apply_to_records_before_rebuild, test_44_rebuild_restores_rules_1_and_3_only), test_compaction.py::test_79_rule2_disabled_by_default, test_compaction.py::test_cli_compact_respects_rule2_flag |
| 80 | пройден | test_compaction.py::test_rule2_requires_record_created_after_rebuild |
| 81 | пройден | test_search.py::test_81_long_utterance_respects_max_search_terms |
| 82 | пройден | test_revision_numbers.py::test_82_record_with_end_delimiter_does_not_break_block |
| 83 | пройден | test_revision_numbers.py::test_83_control_unicode_is_neutralised_before_insert |
| 84 | пройден | test_revision_numbers.py::test_84_record_with_section_headers_is_handled_safely |
| 85 | пройден | test_capture.py::RedactionTests (setUp, test_85_typical_api_keys_replaced, test_85b_bearer_jwt_pem_connection_string, test_85c_key_value_forms, test_86_plain_word_password_is_kept, test_106_placeholder_is_deterministic, test_107_same_secret_same_hash, test_89_no_secret_fragments_in_log, _with_broken_redaction, test_134_redaction_failure_withholds_body, test_135_redaction_timeout_withholds_body, test_136_traceback_not_in_main_log), test_capture.py::test_85_typical_api_keys_replaced, test_capture.py::test_85b_bearer_jwt_pem_connection_string, test_capture.py::test_85c_key_value_forms |
| 86 | пройден | test_capture.py::test_86_plain_word_password_is_kept |
| 87 | пройден | test_revision_numbers.py::test_87_jwt_is_replaced |
| 88 | пройден | test_revision_numbers.py::test_88_pem_private_key_is_replaced |
| 89 | пройден | test_capture.py::RedactionTests (setUp, test_85_typical_api_keys_replaced, test_85b_bearer_jwt_pem_connection_string, test_85c_key_value_forms, test_86_plain_word_password_is_kept, test_106_placeholder_is_deterministic, test_107_same_secret_same_hash, test_89_no_secret_fragments_in_log, _with_broken_redaction, test_134_redaction_failure_withholds_body, test_135_redaction_timeout_withholds_body, test_136_traceback_not_in_main_log), test_capture.py::test_89_no_secret_fragments_in_log |
| 90 | пройден, не обязателен для первой приёмки (§23) | test_ret.py::test_30_selection_uses_last_turn_and_excludes_current_session |
| 91 | пройден, не обязателен для первой приёмки (§23) | test_ret.py::test_unreadable_last_turn_falls_back_to_mtime |
| 92 | пройден, не обязателен для первой приёмки (§23) | test_digest.py::test_92_name_uses_safe_and_hash_suffix |
| 93 | пройден, не обязателен для первой приёмки (§23) | test_stage0_core.py::test_comparison_is_case_insensitive_on_windows |
| 94 | пройден, не обязателен для первой приёмки (§23) | test_stage0_core.py::test_longest_prefix_wins |
| 95 | пройден, не обязателен для первой приёмки (§23) | test_stage0_core.py::test_empty_cwd_gives_common |
| 96 | пройден, не обязателен для первой приёмки (§23) | test_ret.py::test_damaged_digest_is_skipped_and_logged, test_ret.py::test_unreadable_last_turn_falls_back_to_mtime |
| 97 | пройден, не обязателен для первой приёмки (§23) | test_revision_numbers.py::test_97_damaged_session_state_does_not_stop_hermes |
| 98 | пройден, не обязателен для первой приёмки (§23) | test_indexer.py::test_104_verify_reports_duplicate_physical_records |
| 99 | пройден, не обязателен для первой приёмки (§23) | test_revision_numbers.py::test_99_record_without_content_hash_is_not_indexed_as_valid |
| 100 | пройден, не обязателен для первой приёмки (§23) | test_capture.py::test_100_unknown_metadata_key_is_not_damage, test_capture.py::test_100_invalid_enum_is_metadata_error |
| 101 | пройден | test_capture.py::test_101_event_id_stable_on_redelivery |
| 102 | пройден | test_capture.py::IdempotencyTests (setUp, test_2_same_turn_delivered_twice_creates_one_record, test_61_conflicting_redelivery_keeps_single_canonical_record, test_62_conflict_is_logged, test_162_both_versions_stay_in_journal, test_51_capture_disabled_writes_nothing, test_114_disabling_capture_keeps_existing_journal, test_102_capture_survives_unavailable_sqlite), test_capture.py::test_102_capture_survives_unavailable_sqlite |
| 103 | пройден | test_indexer.py::DuplicateAndRevisionTests (_append_physical_duplicate, test_103_physical_duplicate_handled_deterministically, test_115_rebuild_after_duplicate_is_deterministic, test_104_verify_reports_duplicate_physical_records, test_162_revision_replaces_index_entry, test_163_verify_reports_more_than_one_revision, test_64_index_keeps_journal_content_hash), test_indexer.py::test_103_physical_duplicate_handled_deterministically |
| 104 | пройден | test_indexer.py::test_104_verify_reports_duplicate_physical_records |
| 105 | пройден | test_capture.py::SizeLimitTests (setUp, test_105_record_respects_max_memory_record, test_105_metadata_is_never_dropped, test_metadata_survives_truncation, test_63_hash_uses_full_content_not_truncated, test_64_stored_hash_is_not_recomputed_from_body), test_capture.py::test_105_record_respects_max_memory_record |
| 106 | пройден | test_capture.py::RedactionTests (setUp, test_85_typical_api_keys_replaced, test_85b_bearer_jwt_pem_connection_string, test_85c_key_value_forms, test_86_plain_word_password_is_kept, test_106_placeholder_is_deterministic, test_107_same_secret_same_hash, test_89_no_secret_fragments_in_log, _with_broken_redaction, test_134_redaction_failure_withholds_body, test_135_redaction_timeout_withholds_body, test_136_traceback_not_in_main_log), test_capture.py::test_106_placeholder_is_deterministic |
| 107 | пройден | test_capture.py::RedactionTests (setUp, test_85_typical_api_keys_replaced, test_85b_bearer_jwt_pem_connection_string, test_85c_key_value_forms, test_86_plain_word_password_is_kept, test_106_placeholder_is_deterministic, test_107_same_secret_same_hash, test_89_no_secret_fragments_in_log, _with_broken_redaction, test_134_redaction_failure_withholds_body, test_135_redaction_timeout_withholds_body, test_136_traceback_not_in_main_log), test_capture.py::test_107_same_secret_same_hash |
| 108 | пройден | test_search.py::QueryBuildTests (setUp, test_49_short_word_does_not_enter_query, test_49_min_word_len_is_configurable, test_108_zero_terms_after_normalization, test_27_user_text_cannot_change_query_structure, test_50_stem_prefix_is_not_substring_search, test_50_terms_are_limited_and_unique, test_81_long_utterance_respects_max_search_terms, test_query_modes_use_expected_operator), test_search.py::test_108_zero_terms_after_normalization |
| 109 | пройден | test_ret.py::CompactionReturnTests (grow_then_shrink, test_18_return_current_session_digest_after_compaction, test_18_digest_created_on_compaction_when_missing, test_45_decision_uses_thresholds_and_is_logged, test_125_decision_log_carries_all_required_fields, test_45_cooldown_blocks_repeat_detection, test_45_injection_limit_is_enforced_and_logged, test_109_history_length_measured_without_minimem_blocks, test_109_blocks_do_not_shift_compaction_decision), test_ret.py::test_109_history_length_measured_without_minimem_blocks, test_ret.py::test_109_blocks_do_not_shift_compaction_decision |
| 110 | пройден | test_stage7.py::CatchUpFirstTurnTests (prepare_previous, test_74_catch_up_creates_previous_digest_and_clears_pending, test_74_catch_up_skipped_without_previous_session, test_74_previous_session_without_digests_chosen_by_last_seen, test_110_catch_up_not_repeated_when_digest_done, test_74_interrupted_previous_session_gets_completion_field, test_78_catch_up_without_mode_return_creates_no_digest), test_stage7.py::CatchUpIdempotencyTests (prepare_previous, test_76_repeated_catch_up_creates_one_digest, test_76_repeated_catch_up_is_idempotent_for_compaction, test_110_catch_up_skipped_when_digest_valid_and_done, test_110_damaged_digest_is_rebuilt_by_catch_up), test_stage7.py::test_110_catch_up_not_repeated_when_digest_done, test_stage7.py::test_110_catch_up_skipped_when_digest_valid_and_done, test_stage7.py::test_110_damaged_digest_is_rebuilt_by_catch_up |
| 111 | пройден | test_stage7.py::SessionEndLifecycleTests (prepare_session, test_52_digest_created_on_session_end_with_mode_return_false, test_session_end_runs_compaction_before_digest, test_session_end_compaction_disabled_keeps_digest, test_interrupted_session_end_writes_completion, test_158_interrupted_digest_carries_warning_on_insert, test_session_end_hook_process_exits_zero_with_empty_output), test_stage7.py::ModeSwitchTests (prepare_previous, test_55_switching_modes_needs_no_rebuild, test_55_compaction_mode_toggle_keeps_records, test_111_digest_timeout_leaves_return_without_insert, test_111_digest_created_when_budget_sufficient, test_113_cli_compact_ignores_mode_compaction_flag), test_stage7.py::test_111_digest_timeout_leaves_return_without_insert, test_stage7.py::test_111_digest_created_when_budget_sufficient |
| 112 | снят (дубль MM-96) | §23: статус «снят», в прогоне не участвует |
| 113 | пройден | test_stage7.py::SessionEndLifecycleTests (prepare_session, test_52_digest_created_on_session_end_with_mode_return_false, test_session_end_runs_compaction_before_digest, test_session_end_compaction_disabled_keeps_digest, test_interrupted_session_end_writes_completion, test_158_interrupted_digest_carries_warning_on_insert, test_session_end_hook_process_exits_zero_with_empty_output), test_stage7.py::ModeSwitchTests (prepare_previous, test_55_switching_modes_needs_no_rebuild, test_55_compaction_mode_toggle_keeps_records, test_111_digest_timeout_leaves_return_without_insert, test_111_digest_created_when_budget_sufficient, test_113_cli_compact_ignores_mode_compaction_flag), test_stage7.py::test_113_cli_compact_ignores_mode_compaction_flag |
| 114 | пройден | test_capture.py::test_114_disabling_capture_keeps_existing_journal |
| 115 | пройден | test_indexer.py::DuplicateAndRevisionTests (_append_physical_duplicate, test_103_physical_duplicate_handled_deterministically, test_115_rebuild_after_duplicate_is_deterministic, test_104_verify_reports_duplicate_physical_records, test_162_revision_replaces_index_entry, test_163_verify_reports_more_than_one_revision, test_64_index_keeps_journal_content_hash), test_indexer.py::test_115_rebuild_after_duplicate_is_deterministic |
| 116 | пройден | test_capture.py::MemoryBlockStripTests (setUp, block, test_116_block_removed_and_hash_matches_clean_text, test_117_strip_does_not_change_answer_or_truncated, test_118_delimiter_without_intro_is_kept, test_nested_fake_delimiters_do_not_create_second_block, test_unclosed_block_is_not_removed, test_sanitized_delimiters_are_recognised), test_capture.py::test_116_block_removed_and_hash_matches_clean_text |
| 117 | пройден | test_capture.py::MemoryBlockStripTests (setUp, block, test_116_block_removed_and_hash_matches_clean_text, test_117_strip_does_not_change_answer_or_truncated, test_118_delimiter_without_intro_is_kept, test_nested_fake_delimiters_do_not_create_second_block, test_unclosed_block_is_not_removed, test_sanitized_delimiters_are_recognised), test_capture.py::test_117_strip_does_not_change_answer_or_truncated |
| 118 | пройден | test_capture.py::MemoryBlockStripTests (setUp, block, test_116_block_removed_and_hash_matches_clean_text, test_117_strip_does_not_change_answer_or_truncated, test_118_delimiter_without_intro_is_kept, test_nested_fake_delimiters_do_not_create_second_block, test_unclosed_block_is_not_removed, test_sanitized_delimiters_are_recognised), test_capture.py::test_118_delimiter_without_intro_is_kept |
| 119 | пройден | test_revision_numbers.py::test_119_unknown_key_record_is_indexed_with_text |
| 120 | пройден | test_revision_numbers.py::test_120_newer_format_is_indexed_and_only_warned_about |
| 121 | пройден | test_revision_numbers.py::test_121_record_without_fmt_reads_as_format_one |
| 122 | пройден | test_ret.py::test_45_decision_uses_thresholds_and_is_logged |
| 123 | пройден | test_ret.py::test_45_cooldown_blocks_repeat_detection |
| 124 | пройден | test_ret.py::test_45_injection_limit_is_enforced_and_logged |
| 125 | пройден | test_ret.py::test_125_decision_log_carries_all_required_fields |
| 126 | пройден | test_revision_numbers.py::test_126_digest_with_delimiter_does_not_create_second_boundary |
| 127 | пройден | test_revision_numbers.py::test_127_history_measurement_ignores_digest_with_sanitised_delimiter |
| 128 | пройден | test_revision_numbers.py::test_128_catch_up_does_not_start_when_budget_exceeds_remaining |
| 129 | пройден | test_revision_numbers.py::test_129_return_runs_even_when_catch_up_deferred_by_budget |
| 130 | пройден | test_revision_numbers.py::test_130_subbudget_sum_over_deadline_is_rejected |
| 131 | пройден | test_stage8.py::test_missing_hook_registration_is_reported |
| 132 | пройден | test_revision_numbers.py::test_132_rebuilt_old_digest_is_not_selected_as_last |
| 133 | пройден | test_revision_numbers.py::test_133_catch_up_target_is_the_digest_return_would_select |
| 134 | пройден | test_capture.py::RedactionTests (setUp, test_85_typical_api_keys_replaced, test_85b_bearer_jwt_pem_connection_string, test_85c_key_value_forms, test_86_plain_word_password_is_kept, test_106_placeholder_is_deterministic, test_107_same_secret_same_hash, test_89_no_secret_fragments_in_log, _with_broken_redaction, test_134_redaction_failure_withholds_body, test_135_redaction_timeout_withholds_body, test_136_traceback_not_in_main_log), test_capture.py::test_134_redaction_failure_withholds_body |
| 135 | пройден | test_capture.py::RedactionTests (setUp, test_85_typical_api_keys_replaced, test_85b_bearer_jwt_pem_connection_string, test_85c_key_value_forms, test_86_plain_word_password_is_kept, test_106_placeholder_is_deterministic, test_107_same_secret_same_hash, test_89_no_secret_fragments_in_log, _with_broken_redaction, test_134_redaction_failure_withholds_body, test_135_redaction_timeout_withholds_body, test_136_traceback_not_in_main_log), test_capture.py::test_135_redaction_timeout_withholds_body |
| 136 | пройден | test_capture.py::test_136_traceback_not_in_main_log |
| 137 | пройден | test_capture.py::DerivedIdentityTests (setUp, test_137_missing_turn_id_still_saved, test_138_turn_counters_present_in_log, test_missing_session_id_uses_unknown_prefix, test_normal_turn_source_is_hermes), test_capture.py::test_137_missing_turn_id_still_saved |
| 138 | пройден | test_capture.py::DerivedIdentityTests (setUp, test_137_missing_turn_id_still_saved, test_138_turn_counters_present_in_log, test_missing_session_id_uses_unknown_prefix, test_normal_turn_source_is_hermes), test_capture.py::test_138_turn_counters_present_in_log, test_revision_numbers.py::test_138_counters_present_on_empty_event_too |
| 139 | пройден | test_revision_numbers.py::test_139_verify_lists_records_with_derived_turn |
| 140 | пройден | test_stage8.py::StatsTests (test_counts_all_normative_metrics, test_distinguishes_no_hits_from_error, test_search_log_carries_measurement_contract, test_project_filter, test_period_filter, test_broken_log_lines_are_skipped, test_json_output_has_all_sections), test_stage8.py::test_counts_all_normative_metrics |
| 141 | пройден | test_stage8.py::test_search_log_carries_measurement_contract |
| 142 | пройден | test_stage8.py::StatsTests (test_counts_all_normative_metrics, test_distinguishes_no_hits_from_error, test_search_log_carries_measurement_contract, test_project_filter, test_period_filter, test_broken_log_lines_are_skipped, test_json_output_has_all_sections), test_stage8.py::test_distinguishes_no_hits_from_error |
| 143 | пройден | test_search.py::test_query_modes_use_expected_operator |
| 144 | пройден | test_revision_numbers.py::test_144_relative_threshold_is_measured_from_best_score |
| 145 | пройден | test_revision_numbers.py::test_145_explain_prints_scores_without_touching_counters |
| 146 | пройден | test_digest.py::test_146_case_different_sessions_get_different_files |
| 147 | пройден | test_digest.py::test_147_invalid_session_id_is_created_and_found |
| 148 | пройден | test_capture.py::DamagedRecordTests (setUp, write, good_record, test_66_partial_record_keeps_next_valid_record, test_67_nested_begin_marks_previous_damaged, test_69_damaged_record_does_not_stop_others, test_148_partial_append_is_reported_by_size_check, test_100_unknown_metadata_key_is_not_damage, test_100_invalid_enum_is_metadata_error), test_capture.py::test_148_partial_append_is_reported_by_size_check |
| 149 | пройден | test_capture.py::JournalWriteTests (setUp, sample, test_records_separated_by_blank_line, test_offsets_point_to_record_start, test_68_writes_do_not_mix, test_149_damaged_key_is_stable_across_reads, test_body_escaping_roundtrip), test_capture.py::test_149_damaged_key_is_stable_across_reads, test_indexer.py::CursorTests (test_71_skipped_record_indexed_after_failure, test_72_cursor_invalid_when_file_shrinks, test_149_damaged_record_reported_once_per_file_offset, test_73_batch_limit_stops_pass, test_10_indexer_error_does_not_delete_journal_record), test_indexer.py::test_149_damaged_record_reported_once_per_file_offset |
| 150 | пройден | test_revision_numbers.py::test_150_external_rewrite_is_reported |
| 151 | пройден | test_revision_numbers.py::test_151_config_hash_changes_with_parameter_and_keeps_journal |
| 152 | пройден | test_revision_numbers.py::test_152_invariant_violation_reports_config_invalid |
| 153 | пройден | tests/test_required_numbers.py |
| 154 | пройден | tests/test_required_numbers.py |
| 155 | пройден | test_revision_numbers.py::test_155_project_path_is_written_and_normalised |
| 156 | пройден | test_revision_numbers.py::test_156_verify_projects_reports_mapping_mismatch |
| 157 | пройден | test_revision_numbers.py::test_157_reproject_rebinds_with_backup_and_rebuilds_index |
| 158 | пройден | test_stage7.py::test_interrupted_session_end_writes_completion, test_stage7.py::test_158_interrupted_digest_carries_warning_on_insert |
| 159 | пройден | test_digest.py::test_159_missing_status_does_not_block_digest |
| 160 | пройден | test_revision_numbers.py::test_160_sender_and_platform_are_stored_and_do_not_affect_search |
| 161 | пройден | test_revision_numbers.py::test_161_sender_change_is_logged_and_stored_in_record, test_revision_numbers.py::test_161_empty_sender_does_not_raise_event |
| 162 | пройден | test_capture.py::test_162_both_versions_stay_in_journal, test_indexer.py::DuplicateAndRevisionTests (_append_physical_duplicate, test_103_physical_duplicate_handled_deterministically, test_115_rebuild_after_duplicate_is_deterministic, test_104_verify_reports_duplicate_physical_records, test_162_revision_replaces_index_entry, test_163_verify_reports_more_than_one_revision, test_64_index_keeps_journal_content_hash), test_indexer.py::test_162_revision_replaces_index_entry |
| 163 | пройден | test_indexer.py::DuplicateAndRevisionTests (_append_physical_duplicate, test_103_physical_duplicate_handled_deterministically, test_115_rebuild_after_duplicate_is_deterministic, test_104_verify_reports_duplicate_physical_records, test_162_revision_replaces_index_entry, test_163_verify_reports_more_than_one_revision, test_64_index_keeps_journal_content_hash), test_indexer.py::test_163_verify_reports_more_than_one_revision |
| 164 | пройден | test_digest.py::test_164_digest_uses_latest_revision, test_indexer.py::DuplicateAndRevisionTests (_append_physical_duplicate, test_103_physical_duplicate_handled_deterministically, test_115_rebuild_after_duplicate_is_deterministic, test_104_verify_reports_duplicate_physical_records, test_162_revision_replaces_index_entry, test_163_verify_reports_more_than_one_revision, test_64_index_keeps_journal_content_hash) |
| 165 | пройден | test_stage8.py::BackupDoctorTests (test_fresh_backup_reports_no_mismatch, test_stale_backup_reports_date_path_and_age, test_threshold_is_read_from_backup_json, test_default_threshold_when_key_absent, test_no_snapshots_reports_backup_stale, test_incomplete_snapshot_reports_backup_stale, test_missing_settings_file_skips_check, test_snapshot_with_only_config_is_accepted, test_doctor_does_not_modify_operational_files, test_doctor_works_without_sqlite, test_latest_snapshot_by_name), test_stage8.py::test_fresh_backup_reports_no_mismatch |
| 166 | пройден | test_stage8.py::test_stale_backup_reports_date_path_and_age |
| 167 | пройден | test_stage8.py::test_no_snapshots_reports_backup_stale |
| 168 | пройден | test_stage8.py::BackupDoctorTests (test_fresh_backup_reports_no_mismatch, test_stale_backup_reports_date_path_and_age, test_threshold_is_read_from_backup_json, test_default_threshold_when_key_absent, test_no_snapshots_reports_backup_stale, test_incomplete_snapshot_reports_backup_stale, test_missing_settings_file_skips_check, test_snapshot_with_only_config_is_accepted, test_doctor_does_not_modify_operational_files, test_doctor_works_without_sqlite, test_latest_snapshot_by_name), test_stage8.py::test_incomplete_snapshot_reports_backup_stale, test_stage8.py::test_missing_settings_file_skips_check |

## Сводка

- пройден: 146;
- снят (дубль MM-96): 1;
- нет проверки: 0;
- из них вне `required_tests.txt` (не обязательны): 21.

Приёмка не пройдена, если хотя бы один обязательный номер имеет статус
«нет проверки» или если прогон тестов завершился ошибкой (§25 п. 13).
