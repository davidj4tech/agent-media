<?php

declare(strict_types=1);

namespace Drupal\crm_activity\Entity;

use Drupal\Core\Entity\ContentEntityInterface;
use Drupal\Core\Entity\EntityChangedInterface;

/**
 * One contact's activity on one channel on one day.
 */
interface ActivityInterface extends ContentEntityInterface, EntityChangedInterface {

  /**
   * The channels an entry can be on, keyed by machine name.
   */
  public const CHANNELS = [
    'sms' => 'Text',
    'signal' => 'Signal',
    'call' => 'Call',
    'email' => 'Email',
    'note' => 'Note',
  ];

}
