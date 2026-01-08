"""K12 SheerID verifier (synchronous, used by CLI)."""
import re
import random
import logging
import httpx
import datetime
import time
from typing import Dict, Optional, Tuple

try:
    from . import config
    from .name_generator import NameGenerator, generate_email, generate_birth_date
    from .img_generator import generate_teacher_pdf, generate_teacher_png
except ImportError:
    # Allow running as script when module imports differ
    import config
    from name_generator import NameGenerator, generate_email, generate_birth_date
    from img_generator import generate_teacher_pdf, generate_teacher_png

PROGRAM_ID = config.PROGRAM_ID
SHEERID_BASE_URL = config.SHEERID_BASE_URL
MY_SHEERID_URL = config.MY_SHEERID_URL
SCHOOLS = config.SCHOOLS
DEFAULT_SCHOOL_ID = config.DEFAULT_SCHOOL_ID

# Logging
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] [%(levelname)s] %(message)s', datefmt='%H:%M:%S')
logger = logging.getLogger(__name__)


class SheerIDVerifier:
    def __init__(self, verification_id: str):
        self.verification_id = verification_id
        self.device_fingerprint = self._generate_device_fingerprint()
        # Avoid using environment proxies by default to prevent local proxy misrouting
        self.http_client = httpx.Client(timeout=30.0, trust_env=False)

    def __del__(self):
        if hasattr(self, 'http_client'):
            self.http_client.close()

    @staticmethod
    def _generate_device_fingerprint() -> str:
        chars = '0123456789abcdef'
        return ''.join(random.choice(chars) for _ in range(32))

    @staticmethod
    def parse_verification_id(url: str) -> Optional[str]:
        match = re.search(r'verificationId=([a-f0-9]+)', url, re.IGNORECASE)
        if match:
            return match.group(1)
        # fallback pattern
        match = re.search(r'([a-f0-9]{24})', url, re.IGNORECASE)
        if match:
            return match.group(1)
        return None

    def _sheerid_request(self, method: str, url: str, body: Optional[Dict] = None) -> Tuple[Dict, int]:
        headers = {'Content-Type': 'application/json'}
        try:
            response = self.http_client.request(method=method, url=url, json=body, headers=headers)
            try:
                data = response.json()
            except Exception:
                data = response.text
            return data, response.status_code
        except Exception as e:
            logger.error(f"SheerID request failed: {e}")
            raise

    def _upload_to_s3(self, upload_url: str, content: bytes, mime_type: str) -> bool:
        try:
            headers = {'Content-Type': mime_type}
            response = self.http_client.put(upload_url, content=content, headers=headers, timeout=60.0)
            return 200 <= response.status_code < 300
        except Exception as e:
            logger.error(f"S3 upload failed: {e}")
            return False

    def verify(self, first_name: str = None, last_name: str = None, email: str = None, birth_date: str = None, school_id: str = None, resend_email: bool = False) -> Dict:
        try:
            current_step = 'initial'

            if not first_name or not last_name:
                name = NameGenerator.generate()
                first_name = name['first_name']
                last_name = name['last_name']

            school_id = school_id or DEFAULT_SCHOOL_ID
            school = SCHOOLS[school_id]

            if not email:
                email = generate_email()

            if not birth_date:
                birth_date = generate_birth_date()

            logger.info(f"Teacher: {first_name} {last_name}")
            logger.info(f"Email: {email}")
            logger.info(f"School: {school['name']}")
            logger.info(f"Verification ID: {self.verification_id}")

            logger.info("Step 1/4: Generating PDF & PNG...")
            pdf_data = generate_teacher_pdf(first_name, last_name)
            png_data = generate_teacher_png(first_name, last_name)
            pdf_size = len(pdf_data)
            png_size = len(png_data)
            logger.info(f"✓ PDF Size: {pdf_size/1024:.2f}KB, PNG Size: {png_size/1024:.2f}KB")

            logger.info("Step 2/4: Submitting Teacher Info...")

            step2_body = {
                'firstName': first_name,
                'lastName': last_name,
                'birthDate': birth_date,
                'email': email,
                'phoneNumber': '',
                'organization': {
                    'id': school['id'],
                    'idExtended': school['idExtended'],
                    'name': school['name']
                },
                'deviceFingerprintHash': self.device_fingerprint,
                'locale': 'en-US',
                'metadata': {
                    'marketConsentValue': False,
                    'refererUrl': f"{SHEERID_BASE_URL}/verify/{PROGRAM_ID}/?verificationId={self.verification_id}",
                    'verificationId': self.verification_id,
                    'submissionOptIn': 'By submitting the personal information above, I acknowledge that my personal information is being collected under the privacy policy of the business from which I am seeking a discount'
                }
            }

            step2_url = f"{SHEERID_BASE_URL}/rest/v2/verification/{self.verification_id}/step/collectTeacherPersonalInfo"
            step2_data, step2_status = self._sheerid_request('POST', step2_url, step2_body)

            # Handle failure cases: attempt teacher retry without refererUrl first, then
            # if the verification indicates military segment or still fails, try the
            # military step (collectMilitaryStatus) repeatedly until success or max attempts.
            if step2_status != 200:
                # Fetch current verification status to decide fallback
                ver_status = {}
                try:
                    ver_status, _ = self._sheerid_request('GET', f"{MY_SHEERID_URL}/rest/v2/verification/{self.verification_id}")
                except Exception:
                    ver_status = {}

                system_msg = ''
                try:
                    system_msg = str(step2_data.get('systemErrorMessage', '')).lower()
                except Exception:
                    system_msg = str(step2_data).lower()

                is_invalid_step = False
                if isinstance(step2_data, dict) and step2_data.get('errorIds'):
                    is_invalid_step = 'invalidstep' in ','.join(step2_data.get('errorIds', [])).lower()

                should_try_military = False
                if 'localhost' in system_msg or 'cannot perform step' in system_msg or is_invalid_step:
                    should_try_military = True
                if isinstance(step2_data, dict) and step2_data.get('segment') == 'military':
                    should_try_military = True
                if ver_status.get('segment') == 'military' or ver_status.get('currentStep', '').lower().startswith('collectmilitary'):
                    should_try_military = True

                # First attempt: retry teacher without refererUrl if that seems suspect
                if 'refererUrl' in step2_body.get('metadata', {}):
                    modified_body = dict(step2_body)
                    modified_body['metadata'] = dict(modified_body['metadata'])
                    modified_body['metadata'].pop('refererUrl', None)
                    logger.info('Retrying teacher endpoint without refererUrl...')
                    step2_data, step2_status = self._sheerid_request('POST', step2_url, modified_body)

                # If teacher retry still fails and military seems likely, attempt military flow
                if step2_status != 200 and should_try_military:
                    logger.info('Attempting military flow (collectMilitaryStatus) as fallback...')
                    submission_url = ver_status.get('submissionUrl') or f"{SHEERID_BASE_URL}/rest/v2/verification/{self.verification_id}/step/collectMilitaryStatus"
                    statuses = ver_status.get('availableStatuses') or ['ACTIVE_DUTY', 'VETERAN', 'RESERVIST']

                    max_attempts = 4
                    success = False
                    last_error = None
                    for attempt in range(max_attempts):
                        status_choice = random.choice(statuses)
                        # Always POST the status to the dedicated collectMilitaryStatus endpoint
                        status_url = f"{SHEERID_BASE_URL}/rest/v2/verification/{self.verification_id}/step/collectMilitaryStatus"
                        mil_body = {'status': status_choice}
                        logger.info(f"Trying military status set (attempt {attempt+1}/{max_attempts}): status={status_choice} (POST to {status_url})")
                        try:
                            status_data, status_code = self._sheerid_request('POST', status_url, mil_body)
                        except Exception as e:
                            last_error = str(e)
                            logger.info(f"Military status POST failed with exception: {e}")
                            continue

                        if status_code != 200:
                            last_error = status_data
                            logger.info(f"Military status POST returned {status_code}: {status_data}")
                            # If server responded but expects a different flow, continue to next status choice
                            continue

                        logger.info(f"✓ Military status set with status {status_choice}")

                        # Fetch updated verification status to get the correct submission URL for personal info
                        try:
                            ver_status, _ = self._sheerid_request('GET', f"{MY_SHEERID_URL}/rest/v2/verification/{self.verification_id}")
                        except Exception:
                            ver_status = {}

                        current = (ver_status.get('currentStep') or status_data.get('currentStep') or '').lower()
                        submission_url2 = ver_status.get('submissionUrl') or f"{SHEERID_BASE_URL}/rest/v2/verification/{self.verification_id}/step/collectInactiveMilitaryPersonalInfo"

                        # If the flow requires additional personal info (inactive/active military), submit it
                        if current.startswith('collectinactivemilitary') or current.startswith('collectactivemilitary') or 'collectinactivemilitary' in current or 'collectactivemilitary' in current:
                            logger.info('Submitting military personal info (collectInactive/ActiveMilitaryPersonalInfo)...')

                            military_orgs = [
                                {'id': 4070, 'name': 'Army'},
                                {'id': 4073, 'name': 'Air Force'},
                                {'id': 4072, 'name': 'Navy'},
                                {'id': 4071, 'name': 'Marine Corps'},
                                {'id': 4074, 'name': 'Coast Guard'},
                                {'id': 4544268, 'name': 'Space Force'}
                            ]

                            # Try several personal-info permutations per status attempt
                            personal_attempts = 3
                            for p_try in range(personal_attempts):
                                org = random.choice(military_orgs)

                                # Sanitize and (re)generate names/emails if necessary
                                def sanitize_name(n: str) -> str:
                                    return ''.join([c for c in n if c.isalpha() or c == "-"]).strip().title()

                                if not first_name or len(first_name) < 2:
                                    first_name = NameGenerator.generate()['first_name']
                                if not last_name or len(last_name) < 2:
                                    last_name = NameGenerator.generate()['last_name']

                                first_name = sanitize_name(first_name)
                                last_name = sanitize_name(last_name)

                                # Pick conservative birth year and email
                                by = random.randint(1960, 1985)
                                birth_date = f"{by}-{str(random.randint(1,12)).zfill(2)}-{str(random.randint(1,28)).zfill(2)}"

                                min_discharge_year = by + 18
                                max_discharge_year = datetime.date.today().year - 1
                                if min_discharge_year >= max_discharge_year:
                                    dy = max_discharge_year
                                else:
                                    dy = random.randint(min_discharge_year, max_discharge_year)
                                discharge_date = f"{dy}-{str(random.randint(1,12)).zfill(2)}-{str(random.randint(1,28)).zfill(2)}"

                                # regenerate email if server previously complained
                                email = generate_email()

                                personal_body = {
                                    'firstName': first_name,
                                    'lastName': last_name,
                                    'birthDate': birth_date,
                                    'email': email,
                                    'phoneNumber': '',
                                    'organization': {'id': org['id'], 'idExtended': str(org['id']), 'name': org['name']},
                                    'dischargeDate': discharge_date,
                                    'locale': 'en-US',
                                    'country': 'US',
                                    'metadata': {
                                        'marketConsentValue': False,
                                        'refererUrl': f"{SHEERID_BASE_URL}/verify/{PROGRAM_ID}/?verificationId={self.verification_id}",
                                        'verificationId': self.verification_id,
                                        'flags': '{"doc-upload-considerations":"default","doc-upload-may24":"default","doc-upload-redesign-use-legacy-message-keys":false,"docUpload-assertion-checklist":"default","include-cvec-field-france-student":"not-labeled-optional","org-search-overlay":"default","org-selected-display":"default"}',
                                        'submissionOptIn': 'By submitting the personal information above, I acknowledge that my personal information is being collected under the privacy policy of the business from which I am seeking a discount and I understand that my personal information will be shared with SheerID as a processor/third-party service provider in order for SheerID to confirm my eligibility.'
                                    }
                                }

                                logger.info(f"Military personal try {p_try+1}/{personal_attempts}: {personal_body['firstName']} {personal_body['lastName']} birth={personal_body['birthDate']} discharge={personal_body['dischargeDate']} org={personal_body['organization']['id']}")

                                try:
                                    p_data, p_status = self._sheerid_request('POST', submission_url2, personal_body)
                                except Exception as e:
                                    last_error = str(e)
                                    logger.info(f"Military personal info submit failed with exception: {e}")
                                    continue

                                if p_status == 200 and not (isinstance(p_data, dict) and p_data.get('currentStep') == 'error'):
                                    logger.info('✓ Military personal info submitted successfully')
                                    # Treat this as the canonical step 2 response
                                    step2_data, step2_status = p_data, p_status
                                    logger.info(f"Step 2 now uses military personal response (status {step2_status})")
                                    success = True
                                    break

                                # If server returned field-specific errors, try another permutation
                                if isinstance(p_data, dict) and p_data.get('errorIds'):
                                    logger.info(f"Personal submission returned errors: {p_data.get('errorIds')}")
                                    last_error = p_data
                                    continue

                                last_error = p_data

                            if success:
                                break

                        else:
                            # No additional personal info required; treat as success
                            # Use the status response as canonical step2 response
                            step2_data, step2_status = status_data, status_code
                            logger.info(f"Step 2 now uses military status response (status {step2_status})")
                            success = True
                            break

                    if not success:
                        raise Exception(f"Step 2 Failed after military attempts (last: {last_error})")

                # After attempts, if still failing => raise
                if step2_status != 200:
                    raise Exception(f"Step 2 Failed (Status {step2_status}): {step2_data}")

            if step2_data.get('currentStep') == 'error':
                error_msg = ', '.join(step2_data.get('errorIds', ['Unknown error']))
                raise Exception(f"Step 2 Error: {error_msg}")

            logger.info(f"✓ Step 2 Done: {step2_data.get('currentStep')}")
            current_step = step2_data.get('currentStep', current_step)

            if current_step in ['sso', 'collectTeacherPersonalInfo']:
                logger.info("Step 3/4: Bypassing SSO...")
                step3_data, _ = self._sheerid_request('DELETE', f"{SHEERID_BASE_URL}/rest/v2/verification/{self.verification_id}/step/sso")
                logger.info(f"✓ Step 3 Done: {step3_data.get('currentStep')}")
                current_step = step3_data.get('currentStep', current_step)

            # Ensure the verification has progressed to docUpload — poll briefly if needed
            logger.info("Step 4/4: Requesting Upload URLs...")

            try:
                ver_check, _ = self._sheerid_request('GET', f"{MY_SHEERID_URL}/rest/v2/verification/{self.verification_id}")
            except Exception:
                ver_check = {}

            # If server hasn't progressed to docUpload yet, poll up to 3 times
            current = (ver_check.get('currentStep') or '').lower()
            polls = 0
            while polls < 3 and 'docupload' not in current and 'doc_upload' not in current:
                logger.info(f"Verification not yet at docUpload (current: {current}). Retrying in 1s...")
                time.sleep(1)
                try:
                    ver_check, _ = self._sheerid_request('GET', f"{MY_SHEERID_URL}/rest/v2/verification/{self.verification_id}")
                except Exception:
                    ver_check = {}
                current = (ver_check.get('currentStep') or '').lower()
                polls += 1

            # Handle emailLoop by optionally resending email and polling until docUpload
            if 'emailloop' in current:
                if not resend_email:
                    raise Exception("Verification requires email confirmation (emailLoop). Use --resend-email to resend and wait or confirm manually via inbox.")
                if not ver_check.get('canResendEmailLoop'):
                    raise Exception("Verification is in emailLoop but server disallows resending the email. Please confirm the email manually.")
                submission_url = ver_check.get('submissionUrl')
                if not submission_url:
                    raise Exception("Email loop submission URL is missing; cannot resend.")
                logger.info("Resend email requested: POSTing to emailLoop submission URL...")
                try:
                    resend_resp, resend_status = self._sheerid_request('POST', submission_url, {})
                except Exception as e:
                    raise Exception(f"Failed to POST resend email: {e}")
                logger.info(f"Resend email POST returned status {resend_status}")
                # poll for docUpload to appear
                poll_count = 0
                max_polls = 30  # total timeout ~ 60s if sleep 2s
                sleep_sec = 2
                while poll_count < max_polls:
                    try:
                        ver_check, _ = self._sheerid_request('GET', f"{MY_SHEERID_URL}/rest/v2/verification/{self.verification_id}")
                    except Exception:
                        ver_check = {}
                    current = (ver_check.get('currentStep') or '').lower()
                    if 'docupload' in current or 'doc_upload' in current:
                        logger.info("Server progressed to docUpload after resending email.")
                        break
                    logger.info(f"Waiting for docUpload (current: {current}). Retrying in {sleep_sec}s... ({poll_count+1}/{max_polls})")
                    time.sleep(sleep_sec)
                    poll_count += 1
                if poll_count >= max_polls and 'docupload' not in current and 'doc_upload' not in current:
                    raise Exception("Timeout waiting for docUpload after resending email. Please check email inbox or try again later.")

            step4_body = {'files': [
                {'fileName': 'teacher_document.pdf', 'mimeType': 'application/pdf', 'fileSize': pdf_size},
                {'fileName': 'teacher_document.png', 'mimeType': 'image/png', 'fileSize': png_size}
            ]}

            step4_data, step4_status = self._sheerid_request('POST', f"{SHEERID_BASE_URL}/rest/v2/verification/{self.verification_id}/step/docUpload", step4_body)

            # If server didn't return documents, log full response to help debugging
            documents = step4_data.get('documents') or []
            if len(documents) < 2:
                logger.error(f"DocUpload failed (status {step4_status}): {step4_data}")
                # include the verification status snapshot
                try:
                    final_ver, _ = self._sheerid_request('GET', f"{MY_SHEERID_URL}/rest/v2/verification/{self.verification_id}")
                except Exception:
                    final_ver = {}
                logger.error(f"Verification final state: {final_ver}")
                raise Exception('Failed to retrieve Upload URLs')

            pdf_upload_url = documents[0]['uploadUrl']
            png_upload_url = documents[1]['uploadUrl']
            logger.info('✓ Got Upload URLs')

            if not self._upload_to_s3(pdf_upload_url, pdf_data, 'application/pdf'):
                raise Exception('PDF Upload Failed')
            if not self._upload_to_s3(png_upload_url, png_data, 'image/png'):
                raise Exception('PNG Upload Failed')
            logger.info('✓ Docs Uploaded to S3')

            step6_data, _ = self._sheerid_request('POST', f"{SHEERID_BASE_URL}/rest/v2/verification/{self.verification_id}/step/completeDocUpload")
            logger.info(f"✓ Submission Complete: {step6_data.get('currentStep')}")
            final_status = step6_data

            return {'success': True, 'pending': True, 'message': 'Documents submitted, pending review', 'verification_id': self.verification_id, 'redirect_url': final_status.get('redirectUrl'), 'status': final_status}

        except Exception as e:
            logger.error(f"✗ Verification failed: {e}")
            return {'success': False, 'message': str(e), 'verification_id': self.verification_id}
