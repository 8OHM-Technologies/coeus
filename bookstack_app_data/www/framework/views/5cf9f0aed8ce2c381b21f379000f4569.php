<style>
    :root {
        --color-primary: <?php echo e(setting('app-color')); ?>;
        --color-primary-light: <?php echo e(setting('app-color-light')); ?>;
        --color-link: <?php echo e(setting('link-color')); ?>;
        --color-bookshelf: <?php echo e(setting('bookshelf-color')); ?>;
        --color-book: <?php echo e(setting('book-color')); ?>;
        --color-chapter: <?php echo e(setting('chapter-color')); ?>;
        --color-page: <?php echo e(setting('page-color')); ?>;
        --color-page-draft: <?php echo e(setting('page-draft-color')); ?>;
    }
    :root.dark-mode {
        --color-primary: <?php echo e(setting('app-color-dark')); ?>;
        --color-primary-light: <?php echo e(setting('app-color-light-dark')); ?>;
        --color-link: <?php echo e(setting('link-color-dark')); ?>;
        --color-bookshelf: <?php echo e(setting('bookshelf-color-dark')); ?>;
        --color-book: <?php echo e(setting('book-color-dark')); ?>;
        --color-chapter: <?php echo e(setting('chapter-color-dark')); ?>;
        --color-page: <?php echo e(setting('page-color-dark')); ?>;
        --color-page-draft: <?php echo e(setting('page-draft-color-dark')); ?>;
    }
</style>
<?php /**PATH /app/www/resources/views/layouts/parts/custom-styles.blade.php ENDPATH**/ ?>