<?php

declare(strict_types=1);

namespace Drupal\crm_activity\Entity;

use Drupal\Core\Entity\Attribute\ContentEntityType;
use Drupal\Core\Entity\ContentEntityBase;
use Drupal\Core\Entity\ContentEntityDeleteForm;
use Drupal\Core\Entity\ContentEntityForm;
use Drupal\Core\Entity\EntityChangedTrait;
use Drupal\Core\Entity\EntityTypeInterface;
use Drupal\Core\Entity\Routing\AdminHtmlRouteProvider;
use Drupal\Core\Field\BaseFieldDefinition;
use Drupal\Core\StringTranslation\TranslatableMarkup;
use Drupal\crm_activity\ActivityAccessControlHandler;
use Drupal\crm_activity\ActivityListBuilder;
use Drupal\views\EntityViewsData;

/**
 * A contact's activity on one channel on one day.
 *
 * Written by an agent as a rollup ("texted 12 times", "2 calls, 14 min"),
 * never the messages themselves; a note is an entry of its own. The source
 * key makes a write idempotent: the writer looks it up and updates, rather
 * than adding a second entry for the same contact, channel and day.
 */
#[ContentEntityType(
  id: 'crm_activity',
  label: new TranslatableMarkup('Activity'),
  label_collection: new TranslatableMarkup('Activity'),
  label_singular: new TranslatableMarkup('activity entry'),
  label_plural: new TranslatableMarkup('activity entries'),
  label_count: [
    'singular' => '@count activity entry',
    'plural' => '@count activity entries',
  ],
  handlers: [
    'list_builder' => ActivityListBuilder::class,
    'views_data' => EntityViewsData::class,
    'access' => ActivityAccessControlHandler::class,
    'form' => [
      'default' => ContentEntityForm::class,
      'add' => ContentEntityForm::class,
      'edit' => ContentEntityForm::class,
      'delete' => ContentEntityDeleteForm::class,
    ],
    'route_provider' => [
      'html' => AdminHtmlRouteProvider::class,
    ],
  ],
  base_table: 'crm_activity',
  translatable: FALSE,
  admin_permission: 'administer crm activity',
  collection_permission: 'view crm activity',
  entity_keys: [
    'id' => 'id',
    'uuid' => 'uuid',
  ],
  links: [
    'collection' => '/crm/activity',
    'add-form' => '/crm/activity/add',
    'canonical' => '/crm/activity/{crm_activity}',
    'edit-form' => '/crm/activity/{crm_activity}/edit',
    'delete-form' => '/crm/activity/{crm_activity}/delete',
  ],
)]
class Activity extends ContentEntityBase implements ActivityInterface {

  use EntityChangedTrait;

  /**
   * {@inheritdoc}
   */
  public function label(): string {
    $channel = (string) $this->get('channel')->value;
    $name = self::CHANNELS[$channel] ?? $channel;
    return trim($name . ' ' . $this->get('day')->value);
  }

  /**
   * {@inheritdoc}
   */
  public static function baseFieldDefinitions(EntityTypeInterface $entity_type): array {
    $fields = parent::baseFieldDefinitions($entity_type);

    $fields['contact'] = BaseFieldDefinition::create('entity_reference')
      ->setLabel(new TranslatableMarkup('Contact'))
      ->setRequired(TRUE)
      ->setSetting('target_type', 'crm_contact')
      ->setDisplayOptions('form', ['type' => 'entity_reference_autocomplete', 'weight' => 0])
      ->setDisplayOptions('view', ['label' => 'inline', 'type' => 'entity_reference_label', 'weight' => 0])
      ->setDisplayConfigurable('view', TRUE);

    $fields['channel'] = BaseFieldDefinition::create('list_string')
      ->setLabel(new TranslatableMarkup('Channel'))
      ->setRequired(TRUE)
      ->setSetting('allowed_values', self::CHANNELS)
      ->setDisplayOptions('form', ['type' => 'options_select', 'weight' => 1])
      ->setDisplayOptions('view', ['label' => 'inline', 'type' => 'list_default', 'weight' => 1]);

    $fields['day'] = BaseFieldDefinition::create('datetime')
      ->setLabel(new TranslatableMarkup('Day'))
      ->setDescription(new TranslatableMarkup('The day it happened, in the writer\'s time zone.'))
      ->setRequired(TRUE)
      ->setSetting('datetime_type', 'date')
      ->setDisplayOptions('form', ['type' => 'datetime_default', 'weight' => 2])
      ->setDisplayOptions('view', ['label' => 'inline', 'type' => 'datetime_default', 'weight' => 2]);

    $fields['count_in'] = BaseFieldDefinition::create('integer')
      ->setLabel(new TranslatableMarkup('In'))
      ->setDescription(new TranslatableMarkup('Messages or calls from them.'))
      ->setDefaultValue(0)
      ->setSetting('unsigned', TRUE)
      ->setDisplayOptions('form', ['type' => 'number', 'weight' => 3])
      ->setDisplayOptions('view', ['label' => 'inline', 'weight' => 3]);

    $fields['count_out'] = BaseFieldDefinition::create('integer')
      ->setLabel(new TranslatableMarkup('Out'))
      ->setDescription(new TranslatableMarkup('Messages or calls to them.'))
      ->setDefaultValue(0)
      ->setSetting('unsigned', TRUE)
      ->setDisplayOptions('form', ['type' => 'number', 'weight' => 4])
      ->setDisplayOptions('view', ['label' => 'inline', 'weight' => 4]);

    $fields['duration'] = BaseFieldDefinition::create('integer')
      ->setLabel(new TranslatableMarkup('Duration'))
      ->setDescription(new TranslatableMarkup('Seconds on calls that day.'))
      ->setDefaultValue(0)
      ->setSetting('unsigned', TRUE)
      ->setSetting('suffix', ' s')
      ->setDisplayOptions('form', ['type' => 'number', 'weight' => 5])
      ->setDisplayOptions('view', ['label' => 'inline', 'weight' => 5]);

    $fields['summary'] = BaseFieldDefinition::create('string_long')
      ->setLabel(new TranslatableMarkup('Summary'))
      ->setDescription(new TranslatableMarkup('One line, or a note\'s text. Never message bodies.'))
      ->setDisplayOptions('form', ['type' => 'string_textarea', 'weight' => 6])
      ->setDisplayOptions('view', ['label' => 'above', 'weight' => 6]);

    $fields['source_key'] = BaseFieldDefinition::create('string')
      ->setLabel(new TranslatableMarkup('Source key'))
      ->setDescription(new TranslatableMarkup('What wrote it and which rollup it is, e.g. "agent-media:red5:sms:2026-10-18:<contact>". One entry per key.'))
      ->setRequired(TRUE)
      ->setSetting('max_length', 255)
      ->addConstraint('UniqueField')
      ->setDisplayOptions('form', ['type' => 'string_textfield', 'weight' => 7]);

    $fields['created'] = BaseFieldDefinition::create('created')
      ->setLabel(new TranslatableMarkup('Created'));

    $fields['changed'] = BaseFieldDefinition::create('changed')
      ->setLabel(new TranslatableMarkup('Changed'));

    return $fields;
  }

}
