"""
Management command to
./manage.py lms create_orders_for_old_enterprise_course_enrollment
./manage.py lms create_orders_for_old_enterprise_course_enrollment --start-index=0 --end-index=100
./manage.py lms create_orders_for_old_enterprise_course_enrollment --start-index=0 --end-index=100 --batch-size=20
"""

import traceback
from textwrap import dedent

from django.conf import settings
from django.contrib.auth import get_user_model

from django.core.management.base import BaseCommand, CommandError
from opaque_keys.edx.keys import CourseKey
from requests import Timeout
from slumber.exceptions import HttpServerError, SlumberBaseException

from student.models import CourseEnrollment
from openedx.core.djangoapps.commerce.utils import ecommerce_api_client
from enterprise.models import EnterpriseCourseEnrollment

from util.query import use_read_replica_if_available

User = get_user_model()


class Command(BaseCommand):
    """
    Command to back-populate orders(in e-commerce) for the enterprise_course_enrollments.
    """
    help = dedent(__doc__).strip()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        service_user = User.objects.get(username=settings.ECOMMERCE_SERVICE_WORKER_USERNAME)
        self.client = ecommerce_api_client(service_user)

    def _get_enrollments_queryset(self, start_index, end_index):
        """

        Args:
            start_index: start index or None
            end_index:  end index or None

        Returns:
            EnterpriseCourseEnrollments Queryset

        """
        self.stdout.write(u'Getting enrollments from {start} to {end} index (as per command params)'
                          .format(start=start_index or 'start', end=end_index or 'end'))
        enrollments_qs = EnterpriseCourseEnrollment.objects.filter(
            source__isnull=True
        ).order_by('id')[start_index:end_index]
        return use_read_replica_if_available(enrollments_qs)

    def _create_manual_enrollment_orders(self, enrollments):
        """
        Calls ecommerce to create orders for the manual enrollments passed in.

        Returns (success_count, fail_count)
        """
        try:
            order_response = self.client.manual_course_enrollment_order.post(
                {
                    "enrollments": enrollments
                }
            )
        except (SlumberBaseException, ConnectionError, Timeout, HttpServerError) as exc:
            self.stderr.write(
                "\t\t\tFailed to create order for manual enrollments for the following enrollments: {}. Reason: {}"
                .format(enrollments, exc)
            )
            return 0, 0, len(enrollments), []

        order_creations = order_response["orders"]

        successful_creations = []
        failed_creations = []
        new_creations = []
        new_creation_order_numbers = []
        for order in order_creations:
            if order["status"] == "failure":
                failed_creations.append(order)
            elif order["status"] == "success":
                successful_creations.append(order)
                if order["new_order_created"]:
                    new_creations.append(order)
                    new_creation_order_numbers.append(order["detail"])

        if failed_creations:
            self.stderr.write(
                "\t\t\tFailed to created orders for the following manual enrollments. %s",
                failed_creations
            )
        return len(successful_creations), len(new_creations), len(failed_creations), new_creation_order_numbers

    def _is_paid_mode_course_enrollment(self, username, course_id):
        """
            Returns True if mode of the enrollment is paid
        """
        paid_modes = ['verified', 'professional']
        course_key = CourseKey.from_string(course_id)
        enrollment = CourseEnrollment.objects.get(
            user__username=username, course_id=course_key
        )
        return enrollment.mode in paid_modes

    def _get_batched_enrollments(self, enrollments_queryset, offset, batch_size):
        """
        Args:
            enrollments_queryset: enrollments_queryset to slice
            batch_size: slice size

        Returns: enrollments

        """

        self.stdout.write(
            u'\tFetching Enrollments from {start} to {end}'.format(start=offset, end=offset + batch_size)
        )
        enrollments = enrollments_queryset.select_related(
            'enterprise_customer_user', 'enterprise_customer_user__enterprise_customer'
        )[offset: offset + batch_size]
        return enrollments

    def _sync_with_ecommerce(self, enrollments_batch):
        """
        Sync batch of enrollments with ecommerce
        """
        enrollments_payload = []

        non_paid = 0
        invalid = 0

        self.stdout.write(
            u'\t\tProcessing Total : {},'.format(len(enrollments_batch))
        )

        for enrollment in enrollments_batch:
            try:
                enterprise_customer_user = enrollment.enterprise_customer_user
                user = enterprise_customer_user.user
                enterprise_customer = enterprise_customer_user.enterprise_customer
                username = user.username
                course_id = enrollment.course_id
                if not self._is_paid_mode_course_enrollment(username, course_id):
                    # we want to skip this enrollment, as its not paid
                    non_paid += 1
                    continue
                enrollment_payload = {
                    "enterprise_enrollment_id": enrollment.id,
                    "lms_user_id": user.id,
                    "username": username,
                    "email": user.email,
                    "date_placed": enrollment.created.isoformat(),
                    "course_run_key": course_id,
                    "enterprise_customer_name": enterprise_customer.name,
                    "enterprise_customer_uuid": str(enterprise_customer.uuid),
                }
            except AttributeError as ex:
                self.stderr.write(u'\t\tskipping enrollment {} due to invalid data. {}'.format(enrollment.id, ex))
                invalid += 1
                continue
            except CourseEnrollment.DoesNotExist:
                self.stderr.write(u'\t\tskipping enrollment {}, as CourseEnrollment not found'.format(enrollment.id))
                invalid += 1
                continue
            enrollments_payload.append(enrollment_payload)

        self.stdout.write(u'\t\tFound {count} Paid enrollments to sync'.format(count=len(enrollments_payload)))
        if not enrollments_payload:
            return 0, 0, 0, invalid, non_paid, []

        self.stdout.write(u'\t\tSyncing started...')
        success, new, failed, order_numbers = self._create_manual_enrollment_orders(enrollments_payload)
        self.stdout.write(
            u'\t\tSuccess: {} , New: {}, Failed: {}, Invalid:{} , Non-Paid: {}'.format(
                success, new, failed, invalid, non_paid,
            )
        )
        return success, new, failed, invalid, non_paid, order_numbers

    def _sync(self, enrollments_queryset, enrollments_count, enrollments_batch_size):
        """
            Syncs a single site
        """
        self.stdout.write(u'Syncing process started.')

        offset = 0
        enrollments_queue = []
        enrollments_query_batch_size = 1000
        successfully_synced_enrollments = 0
        new_created_orders = 0
        new_created_order_numbers = []
        failed_to_synced_enrollments = 0
        invalid_enrollments = 0
        non_paid_enrollments = 0

        while offset < enrollments_count:
            is_last_iteration = (offset + enrollments_query_batch_size) >= enrollments_count
            self.stdout.write(
                u'\tSyncing enrollments batch from {start} to {end}.'.format(
                    start=offset, end=offset + enrollments_query_batch_size
                )
            )
            enrollments_queue += self._get_batched_enrollments(
                enrollments_queryset,
                offset,
                enrollments_query_batch_size
            )
            while len(enrollments_queue) >= enrollments_batch_size \
                    or (is_last_iteration and enrollments_queue):  # for last iteration need to empty enrollments_queue
                enrollments_batch = enrollments_queue[:enrollments_batch_size]
                del enrollments_queue[:enrollments_batch_size]
                success, new, failed, invalid, non_paid, order_numbers = self._sync_with_ecommerce(enrollments_batch)
                successfully_synced_enrollments += success
                new_created_orders += new
                failed_to_synced_enrollments += failed
                invalid_enrollments += invalid
                non_paid_enrollments += non_paid
                new_created_order_numbers += order_numbers
            self.stdout.write(
                u'\tSuccessfully synced enrollments batch from {start} to {end}'.format(
                    start=offset, end=offset + enrollments_query_batch_size,
                )
            )
            offset += enrollments_query_batch_size

        self.stdout.write(
            u'[Final Summary] Enrollments Success: {}, New: {}, Failed: {}, Invalid: {} , Non-Paid: {}'.format(
                successfully_synced_enrollments, new_created_orders, failed_to_synced_enrollments, invalid_enrollments,
                non_paid_enrollments
            )
        )
        self.stdout.write('New created order numbers {}'.format(new_created_order_numbers))

    def add_arguments(self, parser):
        """
        Definition of arguments this command accepts
        """
        parser.add_argument(
            '--start-index',
            dest='start_index',
            type=int,
            help='Staring index for enrollments',
        )
        parser.add_argument(
            '--end-index',
            dest='end_index',
            type=int,
            help='Ending index for enrollments',
        )
        parser.add_argument(
            '--batch-size',
            default=25,
            dest='batch_size',
            type=int,
            help='Size of enrollments batch to be sent to ecommerce',
        )

    def handle(self, *args, **options):
        """
        Main command handler
        """
        start_index = options['start_index']
        end_index = options['end_index']
        batch_size = options['batch_size']

        try:
            self.stdout.write(u'Command execution started with options = {}.'.format(options))
            enrollments_queryset = self._get_enrollments_queryset(start_index, end_index)
            enrollments_count = enrollments_queryset.count()
            self.stdout.write(u'Total Enrollments count to process: {count}'.format(count=enrollments_count))
            self._sync(enrollments_queryset, enrollments_count, batch_size)

        except Exception as ex:
            traceback.print_exc()
            raise CommandError(u'Command failed with traceback %s' % str(ex))
