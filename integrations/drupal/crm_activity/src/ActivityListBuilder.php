<?php

declare(strict_types=1);

namespace Drupal\crm_activity;

use Drupal\Core\Entity\EntityInterface;
use Drupal\Core\Entity\EntityListBuilder;
use Drupal\crm_activity\Entity\ActivityInterface;

/**
 * Every contact's activity, newest day first.
 */
class ActivityListBuilder extends EntityListBuilder {

  /**
   * {@inheritdoc}
   */
  protected $limit = 100;

  /**
   * {@inheritdoc}
   */
  protected function getEntityIds(): array {
    $query = $this->getStorage()->getQuery()
      ->accessCheck(TRUE)
      ->sort('day', 'DESC')
      ->sort('changed', 'DESC');
    if ($this->limit) {
      $query->pager($this->limit);
    }
    return $query->execute();
  }

  /**
   * {@inheritdoc}
   */
  public function buildHeader(): array {
    return [
      'day' => $this->t('Day'),
      'contact' => $this->t('Contact'),
      'channel' => $this->t('Channel'),
      'counts' => $this->t('In / out'),
      'summary' => $this->t('Summary'),
    ] + parent::buildHeader();
  }

  /**
   * {@inheritdoc}
   */
  public function buildRow(EntityInterface $entity): array {
    return self::cells($entity, TRUE) + parent::buildRow($entity);
  }

  /**
   * The cells of one entry, shared with the contact's Activity tab.
   */
  public static function cells(EntityInterface $entity, bool $with_contact): array {
    $channel = (string) $entity->get('channel')->value;
    $cells = ['day' => $entity->get('day')->value];
    if ($with_contact) {
      $contact = $entity->get('contact')->entity;
      $cells['contact'] = $contact ? $contact->toLink()->toString() : '';
    }
    $cells['channel'] = ActivityInterface::CHANNELS[$channel] ?? $channel;
    $cells['counts'] = $channel === 'note' ? '' : sprintf('%d / %d', $entity->get('count_in')->value, $entity->get('count_out')->value);
    $cells['summary'] = (string) $entity->get('summary')->value;
    return $cells;
  }

}
