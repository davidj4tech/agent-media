<?php

declare(strict_types=1);

namespace Drupal\crm_activity\Controller;

use Drupal\Core\Controller\ControllerBase;
use Drupal\crm\Entity\ContactInterface;
use Drupal\crm_activity\ActivityListBuilder;

/**
 * A contact's Activity tab: their timeline, newest day first.
 */
class ContactActivityController extends ControllerBase {

  /**
   * How many entries the tab shows a page.
   */
  private const PER_PAGE = 50;

  /**
   * The tab.
   */
  public function page(ContactInterface $crm_contact): array {
    $storage = $this->entityTypeManager()->getStorage('crm_activity');
    $ids = $storage->getQuery()
      ->accessCheck(TRUE)
      ->condition('contact', $crm_contact->id())
      ->sort('day', 'DESC')
      ->sort('changed', 'DESC')
      ->pager(self::PER_PAGE)
      ->execute();
    $rows = [];
    foreach ($storage->loadMultiple($ids) as $entity) {
      $rows[] = ActivityListBuilder::cells($entity, FALSE);
    }
    return [
      'table' => [
        '#type' => 'table',
        '#header' => [
          $this->t('Day'),
          $this->t('Channel'),
          $this->t('In / out'),
          $this->t('Summary'),
        ],
        '#rows' => $rows,
        '#empty' => $this->t('No activity yet.'),
      ],
      'pager' => ['#type' => 'pager'],
      '#cache' => [
        'tags' => array_merge($crm_contact->getCacheTags(), ['crm_activity_list']),
        'contexts' => ['user.permissions', 'url.query_args.pagers'],
      ],
    ];
  }

}
