"""
Google Monitoring and Validation Logic

This file contains scripts to monitor user-registered service accounts and
their respective Google projects. The functions in this file will also
handle invalid service accounts and projects.
"""
from cirrus.google_cloud.iam import GooglePolicyMember
from cirrus.google_cloud.errors import GoogleAPIError
from cirrus.google_cloud.iam import GooglePolicy
from cirrus import GoogleCloudManager


from fence.resources.google.validity import (
    GoogleProjectValidity,
    GoogleServiceAccountValidity,
)
from fence.resources.google.utils import get_all_registered_service_accounts
from fence.resources.google.access_utils import (
    force_remove_service_account_from_access,
)

from fence import utils
from fence.errors import NotFound, NotSupported

def validation_check(db):
    """
    Google validation check for all user-registered service accounts
    and projects.

    This will remove any invalid registered service accounts. It will also
    remove all registered service accounts for a given project if the project
    itself is invalid.

    NOTE: This entire function should be time-efficient and finish in less
          than 90 seconds.
          TODO: Test this function with various amounts of service accounts
                and delays from the google API
    """
    registered_service_accounts = get_all_registered_service_accounts(db=db)
    project_service_account_mapping = _get_project_service_account_mapping(
        registered_service_accounts
    )
    for google_project_id, sa_emails in project_service_account_mapping.iteritems():
        print("Validating Google Project: {}".format(google_project_id))
        for sa_email in sa_emails:
            print("    Validating Google Service Account: {}".format(sa_email))
            # Do some basic service account checks, this won't validate
            # the data access, that's done when the project's validated
            if not _is_valid_service_account(sa_email, google_project_id):
                print(
                    "INVALID SERVICE ACCOUNT {} DETECTED. REMOVING...".format(sa_email)
                )
                force_remove_service_account_from_access(
                    sa_email, google_project_id, db=db
                )
                _send_emails_informing_service_account_removal(
                    _get_user_email_list_from_google_project_with_owner_role(
                        google_project_id),
                    [sa_email], google_project_id)

                continue

            print("VALID.")

        if not _is_valid_google_project(google_project_id, db=db):
            # for now, if we detect in invalid project, remove ALL service
            # accounts from access for that project.
            #
            # TODO: If the issue is ONLY a specific service account,
            # it may be possible to isolate it and only remove that
            # from access.
            print(
                "INVALID GOOGLE PROJECT {} DETECTED. "
                "REMOVING ALL SERVICE ACCOUNTS...".format(google_project_id)
            )
            for sa_email in sa_emails:
                force_remove_service_account_from_access(
                    sa_email, google_project_id, db=db
                )
                _send_emails_informing_service_account_removal(
                    _get_user_email_list_from_google_project_with_owner_role(
                        google_project_id),
                    [sa_email], google_project_id)
            continue

        print("VALID.")


def _is_valid_service_account(sa_email, google_project_id):
    """
    Validate the given registered service account and remove if invalid.

    Args:
        sa_email(str): service account email
        google_project_id(str): google project id
    """
    try:
        sa_validity = GoogleServiceAccountValidity(sa_email, google_project_id)
        sa_validity.check_validity(early_return=True)
    except Exception:
        # any issues, assume invalid
        # TODO not sure if this is the right way to handle this...
        sa_validity = False

    return sa_validity


def _is_valid_google_project(google_project_id, db=None):
    """
    Validate the given google project id and remove all registered service
    accounts under that project if invalid.
    """
    try:
        project_validity = GoogleProjectValidity(google_project_id)
        project_validity.check_validity(early_return=True, db=db)
    except Exception:
        # any issues, assume invalid
        # TODO not sure if this is the right way to handle this...
        project_validity = False

    return project_validity


def _get_google_project_ids_from_service_accounts(registered_service_accounts):
    """
    Return a set of just the google project ids that have registered
    service accounts.
    """
    google_projects = set([sa.google_project_id for sa in registered_service_accounts])
    return google_projects


def _get_project_service_account_mapping(registered_service_accounts):
    """
    Return a dict with google projects as keys and a list of service accounts
    as values.

    Example:
    {
        'project_a': [
            'service_acount_a@email.com',
            'service_acount_b@email.com'
        ],
        'project_b': [
            'service_acount_c@email.com',
            'service_acount_d@email.com'
        ]
    }
    """
    output = {}
    for sa in registered_service_accounts:
        if sa.google_project_id in output:
            output[sa.google_project_id].append(sa.email)
        else:
            output[sa.google_project_id] = [sa.email]

    return output


def _get_user_email_list_from_google_project_with_owner_role(project_id):
    """
    Get a list of emails associated to google project id

    Args:
        project_id(str): project id

    Returns:
        list(str): list of emails belong to the project

    """
    try:
        with GoogleCloudManager(project_id, use_default=False) as prj:
            members = prj.get_project_membership(project_id)
            for member in members:
                if not (
                    member.member_type == GooglePolicyMember.SERVICE_ACCOUNT
                    or member.member_type == GooglePolicyMember.USER
                ):
                    raise NotSupported(
                        "Member {} has invalid type: {}".format(
                            member.email_id, member.member_type
                        )
                    )
            users = [
                member
                for member in members
                if member.member_type == GooglePolicyMember.USER
            ]

            return list({
                u.email_id
                for u in users
                for role in u.roles
                if role.name.upper() == "OWNER"})

    except Exception as exc:
        print(
            (
                "validity of Google Project (id: {}) members "
                "could not complete. Details: {}"
            ).format(project_id, exc)
        )
        raise


def _send_emails_informing_service_account_removal(to_emails, service_account_emails, project_id):
    """
    Send emails to list of emails

    Args:
        to_emails(list(str)): list of email addaresses
        service_account_emails(list(str)): service account emails to delete
        project_id(str): google project id

    Returns:
        httpResponse or None: None if input list is empty

    Exceptions:
        ValueError

    """

    if not to_emails:
        return None

    from fence.settings import REMOVE_SERVICE_ACCOUNT_EMAIL_NOTIFICATION
    from_email = REMOVE_SERVICE_ACCOUNT_EMAIL_NOTIFICATION["from"]
    subject = REMOVE_SERVICE_ACCOUNT_EMAIL_NOTIFICATION["subject"]
    text = REMOVE_SERVICE_ACCOUNT_EMAIL_NOTIFICATION["content"]
    domain = REMOVE_SERVICE_ACCOUNT_EMAIL_NOTIFICATION["domain"]
    if REMOVE_SERVICE_ACCOUNT_EMAIL_NOTIFICATION["admin"]:
        to_emails.extend(REMOVE_SERVICE_ACCOUNT_EMAIL_NOTIFICATION["admin"])
    content = text.format(service_account_emails, project_id)

    return utils.send_email(from_email, to_emails, subject, content, domain)

