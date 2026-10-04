# Demonstration Workflow: Express Order Processing

All information in this file is fictional. This is input material and is not included in wiki search.

## Purpose

The ParcelFlow workflow receives orders from the intake queue and routes them for review before assembly.

## Priority rule

If `screening_code` is `EXPRESS`, the workflow sets `priority=urgent` and creates a review task before assembly.

## Missing-data hold

If the review finds missing data, the order receives the `review_hold` status. The hold cannot be released automatically.

## Return to assembly

After the shift manager confirms the data, the workflow moves the order from `review_hold` back to the assembly queue.
