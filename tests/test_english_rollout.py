import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
import ai_rewriter as ai
import ai_batch_processor as batch
import facebook_publisher as pub
import news_policy as policy
import source_enrichment as enrichment
import telegram_collector as collector
from merge_pipeline_state import merge_rows, tid_key

class EnglishRolloutTests(unittest.TestCase):
    def result(self):
        return dict(language='en', facebook_post='Microsoft says it released a security update.',
                    card_title='Microsoft releases a new security update', certainty='attributed')

    def test_end_to_end_english_caption_comment_and_marker(self):
        item=dict(text='Microsoft says it released a security update.',source_links=['https://msrc.microsoft.com/update'])
        result=ai._finalize(self.result(),item)
        result['editorial']={'attention_label':'warning','content_type':'security_alert'}
        message=pub.build_delivery_message(result)
        self.assertTrue(message.startswith('⚠️ Microsoft'))
        self.assertNotIn('\u2067',message)
        self.assertIn('#Cybersecurity',message)
        self.assertEqual(result['first_comment'],'Source: https://msrc.microsoft.com/update')

    def test_arabic_ai_output_cannot_be_relabeled_english(self):
        r=self.result();r['facebook_post']='أعلنت الشركة تحديثا جديدا'
        with self.assertRaises(RuntimeError):
            ai._finalize(r,dict(text='New update',source_links=[]))

    def test_primary_evidence_allows_supported_not_invented_numbers(self):
        item=dict(text='Microsoft says it released a security update.',source_links=[],
                  primary_evidence=[dict(url='https://msrc.microsoft.com/update',text='The update fixes 2 flaws.')])
        r=self.result();r['facebook_post']+=' It fixes 2 flaws.'
        self.assertIn('2 flaws',ai._finalize(r,item)['facebook_post'])
        r=self.result();r['facebook_post']+=' It fixes 99 flaws.'
        with self.assertRaises(RuntimeError): ai._finalize(r,item)

    def test_media_only_is_not_ready_and_never_calls_ai(self):
        with patch.object(batch,'enrich',side_effect=lambda x:x), patch.object(batch,'call_gemini') as call:
            row=batch.prepare_one(dict(telegram_id=999,has_image=True))
        self.assertEqual(row['status'],'needs_editorial_context');call.assert_not_called()

    def test_eligibility_caps_stale_and_old_language(self):
        now=datetime.now(timezone.utc)
        item=dict(language='en',source_published_at=now.isoformat())
        self.assertEqual(policy.eligibility(item,[],now),'')
        self.assertEqual(policy.eligibility(dict(item,language='ar'),[],now),'pending_english_rewrite')
        self.assertEqual(policy.eligibility(dict(item,source_published_at=(now-timedelta(hours=25)).isoformat()),[],now),'stale_source')
        events=[dict(event='published',facebook_post_id=str(i),published_at=now.isoformat()) for i in range(2)]
        self.assertEqual(policy.eligibility(item,events,now),'rolling_hourly_cap')
        self.assertEqual(policy.eligibility(item,events[:1]*3,now),'')

    def test_collector_keeps_missing_datetime_as_null_not_string_none(self):
        rows = collector.parse_messages('''
        <div class="tgme_widget_message" data-post="IntCyberDigest/1796">
          <div class="tgme_widget_message_text">A sourced date is unavailable.</div>
          <time></time>
        </div>
        ''')
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]['published_at'])

    def test_sensitive_claim_requires_public_source_link(self):
        now = datetime.now(timezone.utc)
        item = dict(
            language='en', source_published_at=now.isoformat(),
            facebook_post='A federal judge ruled the warrantless search unconstitutional.',
            card_title='Judge rules on database search', source_url='',
            primary_evidence=[],
        )
        self.assertEqual(
            policy.eligibility(item, [], now),
            'sensitive_claim_without_source',
        )
        linked = dict(item, source_url='https://www.justice.gov/example')
        self.assertEqual(policy.eligibility(linked, [], now), '')
        ordinary = dict(item, facebook_post='A vendor released a security update.', card_title='Security update')
        self.assertEqual(policy.eligibility(ordinary, [], now), '')

    def test_security_competition_results_require_public_source(self):
        now = datetime.now(timezone.utc)
        item = dict(
            language='en', source_published_at=now.isoformat(),
            facebook_post='Pwn2Own results included a successful exploit and a $40,000 payout.',
            card_title='Pwn2Own day two results', source_url='', primary_evidence=[],
        )
        self.assertEqual(
            policy.eligibility(item, [], now),
            'competition_result_without_source',
        )
        linked = dict(item, source_url='https://www.zerodayinitiative.com/blog/results')
        self.assertEqual(policy.eligibility(linked, [], now), '')
        unrelated = dict(item, facebook_post='A vendor released a successful security update.', card_title='Update')
        self.assertEqual(policy.eligibility(unrelated, [], now), '')

    def test_disputed_intrusion_requires_public_source(self):
        now = datetime.now(timezone.utc)
        item = dict(
            language='en', source_published_at=now.isoformat(),
            facebook_post=(
                'A claim surfaced that an AI agent hacked a government portal. '
                'The company said it does not believe AI was involved.'
            ),
            card_title='AI-written warning claim disputed', source_url='',
            primary_evidence=[],
        )
        self.assertEqual(
            policy.eligibility(item, [], now),
            'disputed_intrusion_without_source',
        )
        linked = dict(item, source_url='https://www.pm.gov.au/media/statement')
        self.assertEqual(policy.eligibility(linked, [], now), '')
        product = dict(
            item,
            facebook_post='Microsoft claims its new laptop is twice as fast.',
            card_title='Microsoft laptop benchmark claim',
        )
        self.assertEqual(policy.eligibility(product, [], now), '')

    def test_unsupported_low_information_claim_is_held(self):
        now = datetime.now(timezone.utc)
        item = dict(
            language='en', source_published_at=now.isoformat(),
            facebook_post=(
                'A Telegram message claimed "genetics shaping the world" '
                'without any supporting evidence or context.'
            ),
            card_title='Unverified genetics claim', source_url='',
            primary_evidence=[], editorial={'certainty': 'uncertain'},
        )
        self.assertEqual(
            policy.eligibility(item, [], now),
            'unsupported_low_information_claim',
        )
        linked = dict(item, source_url='https://example.org/research')
        self.assertEqual(policy.eligibility(linked, [], now), '')
        substantive = dict(
            item,
            facebook_post='A vendor said an investigation remains ongoing.',
            card_title='Vendor investigates service disruption',
        )
        self.assertEqual(policy.eligibility(substantive, [], now), '')

    def test_migration_preserves_new_english_even_if_arabic_row_is_larger(self):
        old=dict(telegram_id=1,facebook_post='a'*1000,language='ar')
        new=dict(telegram_id=1,facebook_post='Short English.',language='en',editorial_version=2)
        for remote,local in [(old,new),(new,old)]:
            self.assertEqual(merge_rows([remote],[local],key_fn=tid_key),[new])

    def test_only_exact_https_primary_hosts(self):
        self.assertTrue(enrichment.primary_url('https://www.cisa.gov/news'))
        self.assertTrue(enrichment.primary_url('https://www.zerodayinitiative.com/blog/results'))
        self.assertTrue(enrichment.primary_url('https://www.pm.gov.au/media/statement'))
        for url in ['https://www.cisa.gov.evil.test/news','http://www.cisa.gov/news','https://user@www.cisa.gov/news','http://127.0.0.1','https://www.cisa.gov:999/a']:
            self.assertFalse(enrichment.primary_url(url))

    def test_primary_outage_retains_original_discovery(self):
        item=dict(text='Security update',source_links=['https://www.cisa.gov/news'])
        with patch.object(enrichment.requests,'get',side_effect=enrichment.requests.Timeout):
            result=enrichment.enrich(item)
        self.assertEqual(result['text'],item['text']);self.assertEqual(result['primary_evidence'],[])

    def test_429_records_cooldown_without_retrying_post(self):
        response=SimpleNamespace(status_code=429,headers={'Retry-After':'3600'})
        with patch.object(pub.requests,'request',return_value=response) as send,patch.object(pub,'append_event') as append:
            with self.assertRaises(RuntimeError):pub.api('POST','test','test-token')
        send.assert_called_once()
        self.assertEqual(append.call_args.args[0]['event'],'meta_cooldown')
        self.assertGreater(policy.timestamp(append.call_args.args[0]['retry_at']),pub.now()+timedelta(minutes=59))

    def test_delivery_records_verification_tier_without_claiming_verification(self):
        base = dict(
            source_published_at=datetime.now(timezone.utc).isoformat(),
            editorial={},
        )
        attributed = pub.delivery_metadata(base, 'A reported event.')
        self.assertEqual(attributed['verification_tier'], 'discovery_attribution_only')
        self.assertFalse(attributed['source_link_present'])

        linked = pub.delivery_metadata(
            dict(base, source_url='https://example.com/report'),
            'A linked report.',
        )
        self.assertEqual(linked['verification_tier'], 'linked_external_source')

        primary = pub.delivery_metadata(
            dict(
                base,
                source_url='https://www.cisa.gov/report',
                primary_evidence=[{'url': 'https://www.cisa.gov/report', 'text': 'Evidence'}],
            ),
            'A primary-source report.',
        )
        self.assertEqual(primary['verification_tier'], 'linked_primary_excerpt')
        self.assertEqual(primary['primary_evidence_count'], 1)
