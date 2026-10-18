<?php

declare(strict_types=1);

namespace Drupal\crm_activity;

use Drupal\Core\Access\AccessResult;
use Drupal\Core\Access\AccessResultInterface;
use Drupal\Core\Entity\EntityAccessControlHandler;
use Drupal\Core\Entity\EntityInterface;
use Drupal\Core\Session\AccountInterface;

/**
 * Activity is seen with its permission, and only on a contact one may see.
 */
class ActivityAccessControlHandler extends EntityAccessControlHandler {

  /**
   * The permission each operation needs.
   */
  private const OPERATIONS = [
    'view' => 'view crm activity',
    'update' => 'edit crm activity',
    'delete' => 'delete crm activity',
  ];

  /**
   * {@inheritdoc}
   */
  protected function checkAccess(EntityInterface $entity, $operation, AccountInterface $account): AccessResultInterface {
    $permission = self::OPERATIONS[$operation] ?? NULL;
    if ($permission === NULL) {
      return AccessResult::neutral();
    }
    $result = AccessResult::allowedIfHasPermissions($account, [$permission, 'administer crm activity'], 'OR');
    $contact = $entity->get('contact')->entity;
    if ($contact !== NULL) {
      $result = $result->andIf($contact->access('view', $account, TRUE));
    }
    return $result->addCacheableDependency($entity);
  }

  /**
   * {@inheritdoc}
   */
  protected function checkCreateAccess(AccountInterface $account, array $context, $entity_bundle = NULL): AccessResultInterface {
    return AccessResult::allowedIfHasPermissions($account, ['create crm activity', 'administer crm activity'], 'OR');
  }

}
