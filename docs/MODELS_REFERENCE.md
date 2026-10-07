# Serbisure Database Models

This document outlines the database schema, models, field types, and choices (enums) used in the Serbisure backend.

## Accounts App

### `tbl_user_profile`
- **Database Table:** `accounts_tbl_user_profile`

| Field Name | Data Type | Constraints / Choices / FK |
| --- | --- | --- |
| `password` | `CharField` | - |
| `last_login` | `DateTimeField` | Null=True, Blank=True |
| `is_superuser` | `BooleanField` | - |
| `username` | `CharField` | Unique |
| `date_joined` | `DateTimeField` | - |
| `id` | `UUIDField` | Primary Key, Unique |
| `email` | `EmailField` | Unique |
| `first_name` | `CharField` | - |
| `middle_name` | `CharField` | Null=True, Blank=True |
| `last_name` | `CharField` | - |
| `is_active` | `BooleanField` | - |
| `cancellation_strikes` | `PositiveIntegerField` | - |
| `is_restricted` | `BooleanField` | - |
| `is_staff` | `BooleanField` | - |
| `date_of_birth` | `DateField` | Null=True, Blank=True |
| `religion` | `CharField` | Null=True, Blank=True |
| `nationality` | `CharField` | Null=True, Blank=True |
| `region` | `CharField` | Null=True, Blank=True |
| `province` | `CharField` | Null=True, Blank=True |
| `city` | `CharField` | Null=True, Blank=True |
| `barangay` | `CharField` | Null=True, Blank=True |
| `street` | `CharField` | Null=True, Blank=True |
| `zipcode` | `CharField` | Null=True, Blank=True |
| `country` | `CharField` | Null=True, Blank=True |
| `gender` | `CharField` | Null=True, Blank=True, Choices: ['Male', 'Female', 'Other'] |
| `language` | `CharField` | Null=True, Blank=True |
| `profile_link` | `CharField` | Null=True, Blank=True |
| `resume_url` | `CharField` | Null=True, Blank=True |
| `resume_uploaded_at` | `DateTimeField` | Null=True, Blank=True |
| `account_type` | `CharField` | Choices: ['Kasambahay', 'Homeowner', 'Barangay', 'Admin'] |
| `contact_number` | `CharField` | Unique |
| `show_contact_number` | `BooleanField` | - |
| `is_on_job` | `BooleanField` | - |
| `user_about` | `TextField` | Blank=True |
| `user_tags` | `JSONField` | Blank=True |
| `social_links` | `JSONField` | Blank=True |
| `show_social_links` | `BooleanField` | - |

## Verifications App

### `tbl_documents`
- **Database Table:** `tbl_documents`

| Field Name | Data Type | Constraints / Choices / FK |
| --- | --- | --- |
| `document_id` | `UUIDField` | Primary Key, Unique |
| `user_profile` | `ForeignKey` | FK -> `tbl_user_profile` |
| `verifyBy` | `ForeignKey` | Null=True, Blank=True, FK -> `tbl_user_profile` |
| `document_type` | `CharField` | Choices: ['nbi_clearance', 'police_clearance', 'national_id_front', 'national_id_back'] |
| `document_url` | `CharField` | - |
| `date_issued` | `DateField` | Null=True, Blank=True |
| `valid_until` | `DateField` | Null=True, Blank=True |
| `verification_status` | `CharField` | Choices: ['Unverified', 'Pending', 'Verified', 'Rejected'] |
| `extracted_data` | `JSONField` | Null=True, Blank=True |
| `ocr_raw_text` | `TextField` | Null=True, Blank=True |
| `ocr_match_score` | `FloatField` | Null=True, Blank=True |
| `ocr_discrepancies` | `JSONField` | Blank=True |
| `ocr_processed_at` | `DateTimeField` | Null=True, Blank=True |
| `ocr_retry_count` | `PositiveSmallIntegerField` | - |
| `rejection_reason` | `TextField` | Null=True, Blank=True |
| `document_number` | `CharField` | Null=True, Blank=True |
| `face_liveness_score` | `FloatField` | Null=True, Blank=True |
| `created_at` | `DateTimeField` | Blank=True |

### `tbl_audit_logs`
- **Database Table:** `tbl_audit_logs`

| Field Name | Data Type | Constraints / Choices / FK |
| --- | --- | --- |
| `log_id` | `UUIDField` | Primary Key, Unique |
| `actor` | `ForeignKey` | Null=True, Blank=True, FK -> `tbl_user_profile` |
| `actor_name` | `CharField` | Null=True, Blank=True |
| `actor_email` | `CharField` | Null=True, Blank=True |
| `actor_role` | `CharField` | Null=True, Blank=True |
| `actor_barangay` | `CharField` | Null=True, Blank=True |
| `target_user` | `ForeignKey` | Null=True, Blank=True, FK -> `tbl_user_profile` |
| `target_name` | `CharField` | Null=True, Blank=True |
| `target_email` | `CharField` | Null=True, Blank=True |
| `target_role` | `CharField` | Null=True, Blank=True |
| `target_barangay` | `CharField` | Null=True, Blank=True |
| `document` | `ForeignKey` | Null=True, Blank=True, FK -> `tbl_documents` |
| `document_id_snapshot` | `UUIDField` | Null=True, Blank=True |
| `document_type` | `CharField` | Null=True, Blank=True |
| `document_number` | `CharField` | Null=True, Blank=True |
| `action` | `CharField` | Choices: ['APPROVED', 'REJECTED', 'RESET', 'DELETED', 'UPLOADED', 'REPROCESSED', 'UPDATED'] |
| `previous_status` | `CharField` | Null=True, Blank=True |
| `new_status` | `CharField` | Null=True, Blank=True |
| `reason` | `TextField` | Null=True, Blank=True |
| `ip_address` | `GenericIPAddressField` | Null=True, Blank=True |
| `user_agent` | `CharField` | Null=True, Blank=True |
| `metadata` | `JSONField` | Null=True, Blank=True |
| `created_at` | `DateTimeField` | Blank=True |

## Booking App

### `tbl_booking`
- **Database Table:** `tbl_booking`

| Field Name | Data Type | Constraints / Choices / FK |
| --- | --- | --- |
| `booking_id` | `UUIDField` | Primary Key, Unique |
| `poster_id` | `ForeignKey` | FK -> `tbl_user_profile` |
| `booking_type` | `CharField` | Choices: ['short_term', 'long_term'] |
| `booking_status` | `CharField` | Choices: ['Pending', 'Accepted', 'InProgress', 'Completed', 'Cancelled'] |
| `service_category` | `ArrayField` | - |
| `start_time` | `DateTimeField` | - |
| `end_time` | `DateTimeField` | Null=True, Blank=True |
| `region` | `CharField` | Null=True, Blank=True |
| `province` | `CharField` | Null=True, Blank=True |
| `city` | `CharField` | Null=True, Blank=True |
| `barangay` | `CharField` | Null=True, Blank=True |
| `street` | `CharField` | Null=True, Blank=True |
| `floor_number` | `CharField` | Null=True, Blank=True |
| `zip_code` | `CharField` | Null=True, Blank=True |
| `special_instruction` | `TextField` | Null=True, Blank=True |
| `daily_rate` | `DecimalField` | - |
| `cancelled_by` | `ForeignKey` | Null=True, Blank=True, FK -> `tbl_user_profile` |
| `cancel_requested_by` | `ForeignKey` | Null=True, Blank=True, FK -> `tbl_user_profile` |
| `cancel_requested_at` | `DateTimeField` | Null=True, Blank=True |
| `cancellation_reason` | `CharField` | Null=True, Blank=True |
| `createdAt` | `DateTimeField` | Blank=True |

### `tbl_booking_assignment`
- **Database Table:** `tbl_booking_assignment`

| Field Name | Data Type | Constraints / Choices / FK |
| --- | --- | --- |
| `booking_assignment_id` | `UUIDField` | Primary Key, Unique |
| `booking_id` | `ForeignKey` | FK -> `tbl_booking` |
| `accepter_id` | `ForeignKey` | FK -> `tbl_user_profile` |
| `accepted_at` | `DateTimeField` | Blank=True |

### `tbl_booking_proposal`
- **Database Table:** `tbl_booking_proposal`

| Field Name | Data Type | Constraints / Choices / FK |
| --- | --- | --- |
| `proposal_id` | `UUIDField` | Primary Key, Unique |
| `booking_id` | `ForeignKey` | FK -> `tbl_booking` |
| `proposer_id` | `ForeignKey` | FK -> `tbl_user_profile` |
| `proposed_rate` | `DecimalField` | - |
| `message` | `TextField` | Null=True, Blank=True |
| `status` | `CharField` | Choices: ['Pending', 'Accepted', 'Rejected', 'Withdrawn'] |
| `createdAt` | `DateTimeField` | Blank=True |

## Reviews App

### `tbl_review`
- **Database Table:** `tbl_review`

| Field Name | Data Type | Constraints / Choices / FK |
| --- | --- | --- |
| `review_id` | `UUIDField` | Primary Key, Unique |
| `booking_id` | `ForeignKey` | FK -> `tbl_booking` |
| `reviewer_id` | `ForeignKey` | FK -> `tbl_user_profile` |
| `reviewee_id` | `ForeignKey` | FK -> `tbl_user_profile` |
| `unstructured_feedback` | `TextField` | - |
| `nlp_sentiment` | `CharField` | Choices: ['Positive', 'Neutral', 'Negative'] |
| `rating` | `IntegerField` | - |
| `createdAt` | `DateTimeField` | Blank=True |

## Notifications App

### `tbl_notification`
- **Database Table:** `tbl_notification`

| Field Name | Data Type | Constraints / Choices / FK |
| --- | --- | --- |
| `notification_id` | `UUIDField` | Primary Key, Unique |
| `sender_id` | `ForeignKey` | FK -> `tbl_user_profile` |
| `receiver_id` | `ForeignKey` | FK -> `tbl_user_profile` |
| `notification_message` | `TextField` | - |
| `notification_state` | `CharField` | - |
| `createdAt` | `DateTimeField` | Blank=True |

## Chat App

### `tbl_chat_message`
- **Database Table:** `tbl_chat_message`

| Field Name | Data Type | Constraints / Choices / FK |
| --- | --- | --- |
| `chat_message_id` | `UUIDField` | Primary Key, Unique |
| `sender_id` | `ForeignKey` | FK -> `tbl_user_profile` |
| `receiver_id` | `ForeignKey` | FK -> `tbl_user_profile` |
| `booking_id` | `ForeignKey` | Null=True, Blank=True, FK -> `tbl_booking` |
| `message_type` | `CharField` | Choices: ['text', 'image'] |
| `image_public_id` | `CharField` | Null=True, Blank=True |
| `message_payload` | `EncryptedTextField` | Null=True, Blank=True |
| `is_read` | `BooleanField` | - |
| `is_deleted` | `BooleanField` | - |
| `createdAt` | `DateTimeField` | Blank=True |

### `tbl_chat_reaction`
- **Database Table:** `tbl_chat_reaction`

| Field Name | Data Type | Constraints / Choices / FK |
| --- | --- | --- |
| `reaction_id` | `UUIDField` | Primary Key, Unique |
| `message` | `ForeignKey` | FK -> `tbl_chat_message` |
| `reactor` | `ForeignKey` | FK -> `tbl_user_profile` |
| `emoji` | `CharField` | - |
| `reacted_at` | `DateTimeField` | Blank=True |

